async def after_observation(backend, native, manifest, context):
    """Workflow nodes use core async submit/observe/collect with no extra SSH."""
    return {}
