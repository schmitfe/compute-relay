def after_observation(backend, native, manifest, context):
    """Ordinary workflow jobs need no extra privileged worker operation."""
    return {}


async def after_observation_async(backend, native, manifest, context):
    """Workflow nodes use core async submit/observe/collect with no extra SSH."""
    return {}
