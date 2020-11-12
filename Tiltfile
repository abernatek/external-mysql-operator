
k8s_yaml(kustomize('manifests'))

k8s_yaml(kustomize('example'))

docker_build('abernatek/external-mysql-operator', 'app')
allow_k8s_contexts('docker-desktop')