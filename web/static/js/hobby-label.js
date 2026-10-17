/* #700: the install's word for a hobby, for text and links built in JS.
   base.html puts the label on <html data-hobby-label="{...}"> (web/common.py's hobby_label global,
   core/hobby_label.py Label.as_dict) and loads this file first, so every other script can write
   `HobbyLabel.one` ("Hobby"), `.many` ("Hobbies"), `.one_lc` / `.many_lc` (inside a sentence),
   `.index_url` ("/hobbies") and `.url(slug)` ("/hobby/<slug>"). JS never writes the word itself:
   scripts/check_hobby_label.py fails on a hard-coded one (this file's defaults are the exception). */
(function () {
  var label = {
    one: 'Hobby', many: 'Hobbies', one_lc: 'hobby', many_lc: 'hobbies',
    one_slug: 'hobby', many_slug: 'hobbies', index_url: '/hobbies', is_default: true
  };
  try {
    var raw = document.documentElement.getAttribute('data-hobby-label');
    if (raw) {
      var given = JSON.parse(raw);
      Object.keys(given).forEach(function (k) { label[k] = given[k]; });
    }
  } catch (e) { /* a page without the attribute keeps the default words */ }
  label.url = function (slug) { return '/' + label.one_slug + '/' + encodeURIComponent(slug); };
  window.HobbyLabel = label;
})();
