from django import template

from compute_relay.extensions import metadata_items

register = template.Library()


@register.simple_tag
def extension_navigation():
    return [{"label": label, "url": url} for label, url in metadata_items("NAV_LINKS")]


@register.simple_tag
def project_extension_links(project):
    return [{"label": label, "url": url.format(project_id=project.id)}
            for label, url in metadata_items("PROJECT_LINKS")]
