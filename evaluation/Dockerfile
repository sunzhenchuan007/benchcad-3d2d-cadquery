ARG CAD_IMAGE=benchcad-cadquery:local
FROM ${CAD_IMAGE}
USER root
RUN pip install --no-cache-dir trimesh==4.7.4 shapely==2.1.2 && pip check
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/python"]
