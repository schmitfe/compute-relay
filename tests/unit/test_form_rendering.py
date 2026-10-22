"""Validation messages stay attached to inputs and checkbox groups."""

from django import forms
from django.template.loader import render_to_string


class ExampleForm(forms.Form):
    count = forms.IntegerField(min_value=1, label="CPU count", help_text="Count logical CPUs.")
    clusters = forms.MultipleChoiceField(choices=[("lab", "Lab")], widget=forms.CheckboxSelectMultiple)


def test_shared_configuration_form_links_errors_to_fields_and_groups():
    form = ExampleForm({"count": "0", "clusters": ["unknown"]})
    assert not form.is_valid()
    page = render_to_string("broker/configuration_form.html", {"form": form})
    for name in ("count", "clusters"):
        assert f'href="#id_{name}"' in page
        assert f'id="id_{name}_error"' in page
        assert page.index(f'id="id_{name}"') < page.index(f'id="id_{name}_error"')
    assert 'aria-describedby="id_count_helptext id_count_error"' in page
    assert 'aria-invalid="true"' in page
    assert 'href="#"' not in page
