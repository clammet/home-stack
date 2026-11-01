"""Deployment contracts shared by the builder and standalone controller."""

# Dependencies from Immich v3.3.5's release compose file. Database majors must
# only change through an explicit migration, never by following a latest tag.
IMMICH_DATABASE = "ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0@sha256:bcf63357191b76a916ae5eb93464d65c07511da41e3bf7a8416db519b40b1c23"
IMMICH_REDIS = "docker.io/valkey/valkey:9@sha256:c123e3715db63d06d4ad6964884037aa0d5d4d703939b9929954112889708e1d"


def image_references(app):
    if app["kind"] != "immich":
        return {"server": app["image"]}
    settings = app["settings"]
    worker = settings["machine_learning_image"]
    if worker is None:
        prefix = "ghcr.io/immich-app/immich-server:"
        if not app["image"].startswith(prefix) or "@" in app["image"]:
            raise ValueError("Custom or digest-pinned Immich images require settings.machine_learning_image")
        worker = "ghcr.io/immich-app/immich-machine-learning:" + app["image"][len(prefix):]
    return {"server": app["image"], "machine-learning": worker,
            "database": settings["database_image"], "redis": settings["redis_image"]}


def deployment_name(app, component):
    return app["name"] + ("" if component == "server" else "-" + component)


def unifi_ports(app):
    settings = app["settings"]
    ports = [f"{settings['web_port']}:8443/tcp", "8080:8080/tcp", "3478:3478/udp", "10001:10001/udp"]
    if settings["guest_portal"]:
        ports.extend(("8880:8880/tcp", "8843:8843/tcp"))
    return ports
