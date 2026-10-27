"""Grouping adds no remote operation or alternate execution authority."""


async def after_observation_async(backend, native, manifest, context):
    return {}
