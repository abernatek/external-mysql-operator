#!/usr/bin/python3

import base64
import os
import random
import string
import time

import kopf
import mysql.connector
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from mysql.connector import Error

config.load_incluster_config()
operator_namespace = value = os.getenv('OPERATOR_NAMESPACE', 'external-mysql-operator') 

## HANDLING OBJECTS

@kopf.on.create('mysql.getresponse.com', 'v1', 'databases')
def create_fn(spec, name, namespace, logger, **kwargs):

    # CHECK IF POINTED INSTANCE EXISTS
    instance = get_custom_object(name=spec['instance'], plural='instances', namespace=operator_namespace)
    if instance:

        # CREATE DATABASE
        query_instance("CREATE DATABASE IF NOT EXISTS %s" % name, instance)
        logger.info("created database named %s" % name)

        # CREATE SERVICE IN CUSTOM RESOURCE DATABASE'S NAMESPACE
        service = yaml.safe_load(f"""
            kind: Service
            apiVersion: v1
            metadata:
              name: {name}
            spec:
              type: ExternalName
              externalName: {instance['spec']['address']}
              ports:
              - port: {instance['spec']['port']}
        """)
        kopf.adopt(service)
        try:
          service = client.CoreV1Api().create_namespaced_service(namespace=service['metadata']['namespace'], body=service)
        except ApiException as e:
          raise kopf.PermanentError(e)

        # RUN INITDB JOB IF SPECIFIED
        if 'initDb' in spec:
          logger.info(init_db(spec['initDb'], instance, name, namespace))

        # ADD USERS AND GRANT THEM PRIVILEGES
        for user in spec['users']:
          create_user(user, instance, name, namespace)

    else:
      raise kopf.PermanentError('no such instance %s' % spec['instance'])

@kopf.on.delete('mysql.getresponse.com', 'v1', 'databases')
def delete_fn(spec, name, logger, **kwargs):

  instance = get_custom_object(name=spec['instance'], plural='instances', namespace=operator_namespace)
  if instance:

    # DROP DATABASE IF DESIRED
    if spec['dropOnDelete']:
      result = query_instance("DROP DATABASE IF EXISTS %s" % name, instance)
      logger.info("dropped database named %s" % name)

    else:
      logger.info("database named %s retained" % name)
    
    # DROP USERS
    for user in spec['users']:
      drop_user(user, instance, name)

  else:
    raise kopf.PermanentError('no such instance %s' % spec['instance'])

## HELPERS

def create_user(user, instance, name, namespace, **kwargs):

    if 'password' in user:
      password = user['password']
    else:
      password = generate_password()

    secret = yaml.safe_load(f"""
        kind: Secret
        apiVersion: v1
        metadata:
          name: {name}-{user['username']}
          namespace: {namespace}
        data:
          DB_USER_{name.upper()}: {base64.b64encode(user['username'].encode()).decode('utf-8')}
          DB_PASSWORD_{name.upper()}: {base64.b64encode(password.encode()).decode('utf-8')}
          DB_HOST_{name.upper()}: {base64.b64encode(name.encode()).decode('utf-8')}
    """)
    kopf.adopt(secret)
    try:
      secret = client.CoreV1Api().create_namespaced_secret(namespace=namespace, body=secret)
    except ApiException as e:
      raise kopf.PermanentError(e)

    drop_user(user, instance, name)
    query_instance("CREATE USER '%s'@'%s' IDENTIFIED BY '%s'" % (user['username'], user['allowedHosts'], password), instance)

    privileges = ",".join(user['privileges'])
    for table in user['tables']:
      query_instance("GRANT %s ON %s.%s TO '%s'@'%s'" % (privileges, name, table, user['username'], user['allowedHosts']), instance)

def drop_user(user, instance, name, **kwargs):

    # check if user exists ( can't use CREATE USER IF NOT EXISTS becouse it's not compatible with MySQL < 5.7.6 )
    if query_instance("SELECT User,Host FROM mysql.user WHERE User = '%s' AND Host = '%s';" % (user['username'], user['allowedHosts']), instance):
      query_instance("DROP USER '%s'@'%s'" % (user['username'], user['allowedHosts']), instance)

def get_value_from_secret(key, name, namespace, **kwargs):

    try:
      secret = client.CoreV1Api().read_namespaced_secret(namespace=namespace,name=name).data
      return (base64.b64decode(secret[key]).decode('utf-8'))
    except ApiException as e:
        raise kopf.PermanentError(e)

def get_custom_object(plural, name, namespace, **kwargs):

    try:
      return(client.CustomObjectsApi().get_namespaced_custom_object(
            group='mysql.getresponse.com',
            version='v1',
            namespace=namespace,
            plural=plural,
            name=name))
    except ApiException as e:
        raise kopf.PermanentError(e)

def generate_password(size=16, chars=string.ascii_uppercase + string.digits + string.ascii_lowercase):

    return ''.join(random.choice(chars) for _ in range(size))

def init_db(initDb, instance, name, namespace, **kwargs):

    configMap = yaml.safe_load(f"""
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: initdbscript-{name}
    """)
    configMap['data'] = {}
    configMap['data']['initdbscript'] = initDb['initDbScript']

    kopf.adopt(configMap)
    try:
      configMap = client.CoreV1Api().create_namespaced_config_map(namespace=namespace, body=configMap)
    except ApiException as e:
       raise kopf.PermanentError(e)

    user = {}
    user['username'] = "initdb"
    user['allowedHosts'] = "%"
    user['tables'] = ["*"]
    user['privileges'] = ["ALL PRIVILEGES"]

    create_user(user, instance, name, namespace)

    initJob = yaml.safe_load(f"""
        kind: Job
        apiVersion: batch/v1
        metadata:
          name: initdb-{name}
        spec:
          backoffLimit: 0
          template:
            spec:
              containers:
              - name: initdb-{name}
                image: {initDb['initDbImage']}
                command: ['/bin/bash', '-c']
                args: ["/bin/initdbscript"]
                envFrom:
                - secretRef:
                    name: {name}-initdb
                volumeMounts:
                  - name: initdbscript
                    mountPath: /bin/initdbscript
                    subPath: initdbscript
              restartPolicy: Never
              volumes:
              - name: initdbscript
                configMap:
                  name: initdbscript-{name}
                  defaultMode: 0777
    """)
    kopf.adopt(initJob)
    try:
      initJob = client.BatchV1Api().create_namespaced_job(namespace=namespace, body=initJob)
    except ApiException as e:
      raise kopf.PermanentError(e)
    init_job = wait_for_job_completion("initdb-%s" % name, namespace)
    drop_user(user, instance, name)
    return init_job

def query_instance(query, instance, **kwargs):

    user = get_value_from_secret(key="INSTANCE_USER", name=instance['spec']['secretName'], namespace=instance['metadata']['namespace'])
    password = get_value_from_secret(key="INSTANCE_PASSWORD",name=instance['spec']['secretName'], namespace=instance['metadata']['namespace'])
    try:
        connection = mysql.connector.connect(host=instance['spec']['address'],
                                            port=instance['spec']['port'],
                                            user=user,
                                            password=password)
        if connection.is_connected():
            cursor = connection.cursor()
            cursor.execute(query)
            return(cursor.fetchone())
    except Error as e:
      raise kopf.PermanentError(e)

def wait_for_job_completion(job, namespace, timeout=300):

    start = time.time()
    while time.time() - start < timeout:
        time.sleep(2)
        response = client.BatchV1Api().read_namespaced_job_status(job, namespace)
        if (response.status.completion_time != None):
            return ("Database init job done.")
        if (response.status.failed != None):
          raise kopf.PermanentError('Job %s failed' % job)
        else:
            continue
    raise kopf.PermanentError('Waiting timeout for job %s' % job)