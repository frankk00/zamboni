import os
import re

from django import forms
from django.conf import settings
from django.forms.models import modelformset_factory
from django.template import Context, Template, TemplateSyntaxError

import happyforms
from tower import ugettext_lazy as _lazy
from quieter_formset.formset import BaseModelFormSet

import amo
import product_details
from amo.urlresolvers import reverse
from applications.models import Application, AppVersion
from bandwagon.models import Collection, FeaturedCollection
from zadmin.models import ValidationJob


class BulkValidationForm(happyforms.ModelForm):
    application = forms.ChoiceField(
                label=_lazy(u'Application'),
                choices=[(a.id, a.pretty) for a in amo.APPS_ALL.values()])
    curr_max_version = forms.ChoiceField(
                label=_lazy(u'Current Max. Version'),
                choices=[('', _lazy(u'Select an application first'))])
    target_version = forms.ChoiceField(
                label=_lazy(u'Target Version'),
                choices=[('', _lazy(u'Select an application first'))])
    finish_email = forms.CharField(required=False,
                                   label=_lazy(u'Email when finished'))

    class Meta:
        model = ValidationJob
        fields = ('application', 'curr_max_version', 'target_version',
                  'finish_email')

    def __init__(self, *args, **kw):
        kw.setdefault('initial', {})
        kw['initial']['finish_email'] = settings.FLIGTAR
        super(BulkValidationForm, self).__init__(*args, **kw)
        w = self.fields['application'].widget
        # Get the URL after the urlconf has loaded.
        w.attrs['data-url'] = reverse('zadmin.application_versions_json')

    def version_choices_for_app_id(self, app_id):
        versions = AppVersion.objects.filter(application__id=app_id)
        return [(v.id, v.version) for v in versions]

    def clean_application(self):
        app_id = int(self.cleaned_data['application'])
        app = Application.objects.get(pk=app_id)
        self.cleaned_data['application'] = app
        choices = self.version_choices_for_app_id(app_id)
        self.fields['target_version'].choices = choices
        self.fields['curr_max_version'].choices = choices
        return self.cleaned_data['application']

    def _clean_appversion(self, field):
        return AppVersion.objects.get(pk=int(field))

    def clean_curr_max_version(self):
        return self._clean_appversion(self.cleaned_data['curr_max_version'])

    def clean_target_version(self):
        return self._clean_appversion(self.cleaned_data['target_version'])


path = os.path.join(settings.ROOT, 'apps/zadmin/templates/zadmin')
texts = {
    'success': open('%s/%s' % (path, 'success.txt')).read(),
    'failure': open('%s/%s' % (path, 'failure.txt')).read(),
}


varname = re.compile(r'{{\s*([a-zA-Z0-9_]+)\s*}}')


class NotifyForm(happyforms.Form):
    subject = forms.CharField(widget=forms.TextInput, required=True)
    preview_only = forms.BooleanField(initial=True, required=False,
                            label=_lazy(u'Log emails instead of sending'))
    text = forms.CharField(widget=forms.Textarea, required=True)
    variables = ['{{ADDON_NAME}}', '{{ADDON_VERSION}}', '{{APPLICATION}}',
                 '{{COMPAT_LINK}}', '{{RESULT_LINKS}}', '{{VERSION}}']
    variable_names = [varname.match(v).group(1) for v in variables]

    def __init__(self, *args, **kw):
        kw.setdefault('initial', {})
        if 'text' in kw:
            kw['initial']['text'] = texts[kw.pop('text')]
        kw['initial']['subject'] = ('{{ADDON_NAME}} {{ADDON_VERSION}} '
                                    'compatibility with '
                                    '{{APPLICATION}} {{VERSION}}')
        super(NotifyForm, self).__init__(*args, **kw)

    def check_template(self, data):
        try:
            Template(data).render(Context({}))
        except TemplateSyntaxError, err:
            raise forms.ValidationError(err)
        for name in varname.findall(data):
            if name not in self.variable_names:
                raise forms.ValidationError(
                            u'Variable {{%s}} is not a valid variable' % name)
        return data

    def clean_text(self):
        return self.check_template(self.cleaned_data['text'])

    def clean_subject(self):
        return self.check_template(self.cleaned_data['subject'])


class FeaturedCollectionForm(happyforms.ModelForm):
    LOCALES = (('', u'(Default Locale)'),) + tuple(
        (i, product_details.languages[i]['native'])
        for i in settings.AMO_LANGUAGES)

    application = forms.ModelChoiceField(Application.objects.all())
    collection = forms.CharField(widget=forms.HiddenInput)
    locale = forms.ChoiceField(choices=LOCALES, required=False)

    class Meta:
        model = FeaturedCollection
        fields = ('application', 'locale')

    def clean_collection(self):
        application = self.cleaned_data.get('application', None)
        collection = self.cleaned_data.get('collection', None)
        if not Collection.objects.filter(id=collection,
                                         application=application).exists():
            raise forms.ValidationError(
                u'Invalid collection for this application.')
        return collection

    def save(self, commit=False):
        collection = self.cleaned_data['collection']
        f = super(FeaturedCollectionForm, self).save(commit=commit)
        f.collection = Collection.objects.get(id=collection)
        f.save()
        return f


class BaseFeaturedCollectionFormSet(BaseModelFormSet):

    def __init__(self, *args, **kw):
        super(BaseFeaturedCollectionFormSet, self).__init__(*args, **kw)
        for form in self.initial_forms:
            try:
                form.initial['collection'] = (FeaturedCollection.objects
                    .get(id=form.instance.id).collection.id)
            except (FeaturedCollection.DoesNotExist, Collection.DoesNotExist):
                form.initial['collection'] = None


FeaturedCollectionFormSet = modelformset_factory(FeaturedCollection,
    form=FeaturedCollectionForm, formset=BaseFeaturedCollectionFormSet,
    can_delete=True, extra=0)
