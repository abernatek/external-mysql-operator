
k8s_yaml(kustomize('manifests'))

k8s_yaml(kustomize('example'))

docker_build('mefi5to/external-mysql-operator', 'app')
allow_k8s_contexts('docker-desktop')