FROM debian:jessie
MAINTAINER simon@simonluijk.com

RUN echo 'DPkg::Post-Invoke {"/bin/rm -f /var/cache/apt/archives/*.deb || true";};' | tee /etc/apt/apt.conf.d/no-cache && \
    apt-get update -y && \
    apt-get clean && \
    rm -rf /var/cache/apt/* && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y git python-pip

RUN DEBIAN_FRONTEND=noninteractive apt-get install -y wget && \
    pip install git+https://github.com/ross-urban/aws-ecs-service-discovery.git#egg=ecs_discovery && \
    pip install Jinja2 simplejson boto3

CMD ["bash"]
