"""Advanced pages keep their authorized project and a short route to settings."""

from html import escape

import pytest

from compute_relay.control.models import ConnectorTask, Project
from compute_relay.extensions.investigations.models import Investigation
from compute_relay.extensions.workflows.models import WorkflowRevision, WorkflowRun
from test_support.unit_test_management import form_post, human, key_data, management, prepared
from tests.unit.test_management import project_data


@pytest.mark.parametrize("suffix,current", [
    ("environment-builds/", "Environment builds"),
    ("environment-build-policy/", "Environment build permission"),
    ("storage/", "Storage"),
    ("storage/targets/new/", "Add location"),
    ("workflows/", "Workflows"),
    ("investigations/", "Investigations"),
    ("keys/new/", "Create API key"),
    ("keys/{key_id}/edit/", "Edit key access"),
])
def test_nested_pages_keep_named_project_parent_and_settings_shortcuts(management, suffix, current):
    context = management
    context.project.name = "Project <one> & analysis"
    context.project.save()
    base = f"/projects/{context.project.id}/"
    response = human(context.owner).get(base + suffix.format(key_id=context.credential.id))
    assert response.status_code == 200
    html = response.content.decode()
    breadcrumb = html.split('<nav class="breadcrumbs" aria-label="Breadcrumb">', 1)[1].split("</nav>", 1)[0]
    assert f'href="{base}">Project: {escape(context.project.name)}' in breadcrumb
    assert f'<span aria-current="page">{current}</span>' in breadcrumb
    assert f'<a href="{base}">Back to project</a>' in html
    assert f'href="{base}#project-settings">Project settings</a>' in html
    assert f'href="{base}#api-keys">Agent keys</a>' in html
    assert f'href="{base}environment-build-policy/"' in html
    assert "Settings and optional tools for Project &lt;one&gt; &amp; analysis." in html
    assert not ConnectorTask.objects.exists()


def test_overview_groups_optional_links_below_project_identity(management, settings):
    base = f"/projects/{management.project.id}/"
    browser = human(management.owner)
    html = browser.get(base).content.decode()
    assert html.index("Project: managed project") < html.index("Advanced project settings")
    assert '<details class="project-advanced-settings" id="advanced-project-settings">' in html
    assert html.index('aria-label="Project sections"') < html.index('id="advanced-project-settings"')
    assert html.count('id="advanced-project-settings"') == 1
    assert 'href="#advanced-project-settings"' not in html
    assert "Environment build permission" in html and "Environment build policy" not in html
    settings.BROKER_EXTENSIONS = []
    html = browser.get(base).content.decode()
    assert "Advanced project settings" not in html
    assert "environment-build-policy/" not in html and "#advanced-project-settings" not in html


def test_deeper_optional_records_keep_project_and_immediate_parent(management):
    context = management
    revision = WorkflowRevision.objects.create(project=context.project, name="Analysis workflow", document={}, sha256="a" * 64)
    run = WorkflowRun.objects.create(revision=revision, credential=context.credential)
    investigation = Investigation.objects.create(project=context.project, name="Analysis question", plan={})
    browser = human(context.owner)
    base = f"/projects/{context.project.id}/"
    for path, parent, current in [
        (f"/workflows/{run.id}/", "workflows", revision.name),
        (f"/investigations/{investigation.id}/", "investigations", investigation.name),
    ]:
        response = browser.get(path)
        assert response.status_code == 200
        html = response.content.decode()
        breadcrumb = html.split('<nav class="breadcrumbs" aria-label="Breadcrumb">', 1)[1].split("</nav>", 1)[0]
        assert f'href="{base}{parent}/"' in breadcrumb
        assert f'<span aria-current="page">{current}</span>' in breadcrumb
        assert f'<a href="{base}">Back to project</a>' in html


def test_job_review_and_one_time_key_page_keep_the_same_project_context(management):
    context = management
    browser = human(context.owner)
    job = prepared(context)
    response = browser.get(f"/review/{job.id}/")
    assert f'href="/projects/{context.project.id}/">Back to project</a>'.encode() in response.content
    response = form_post(browser, f"/projects/{context.project.id}/keys/new/", key_data(context))
    assert response.status_code == 201
    assert b'<span aria-current="page">API key created</span>' in response.content
    assert f'href="/projects/{context.project.id}/#api-keys"'.encode() in response.content
    assert b'no-store' in response.headers['Cache-Control'].encode()


@pytest.mark.parametrize("action,anchor", [("settings", "project-settings"), ("edit-key", "api-keys"), ("revoke-key", "api-keys")])
def test_saves_return_to_the_edited_project_section(management, action, anchor):
    context = management
    base = f"/projects/{context.project.id}/"
    path, data = {
        "settings": (base, project_data(context)),
        "edit-key": (base + f"keys/{context.credential.id}/edit/", key_data(context)),
        "revoke-key": (base + f"keys/{context.credential.id}/revoke/", {}),
    }[action]
    response = form_post(human(context.owner), path, data)
    assert response.status_code == 302
    assert response.url == base + "#" + anchor


def test_context_never_reveals_another_owners_project(management):
    context = management
    foreign = Project.objects.create(owner=context.outsider, name="Private foreign project")
    response = human(context.owner).get(f"/projects/{foreign.id}/workflows/")
    assert response.status_code == 403
    assert foreign.name.encode() not in response.content
    assert str(foreign.id).encode() not in response.content
