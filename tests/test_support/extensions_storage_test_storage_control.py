from compute_relay.extensions.storage import services

def enroll(resources):
    return services.register_target(resources.owner, resources.connection, "staging-root-0", "Approved data", project=resources.project)
