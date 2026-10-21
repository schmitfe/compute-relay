def after_observation(backend, native, manifest, context):
    """Ordinary workflow jobs need no extra privileged worker operation."""
    return {}
