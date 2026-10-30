three.js 0.170.0 (r170), ES module builds, vendored for the STL object page's 3D viewer (#742).
Project: https://github.com/mrdoob/three.js   License: MIT (LICENSE, this folder)

Files, byte-identical to npm three@0.170.0 (kept that way by .gitattributes `-text`):
  three.module.min.js  https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.min.js
                       sha256 08fd7545d13d2c7fb65ab691530a802dafefd638596501854f267d0fb13c39e7
  STLLoader.js         https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/loaders/STLLoader.js
                       sha256 a0a83c88b269c94e25b690fae770d350c4728c81853195186976be7af0f8a3b3
  OrbitControls.js     https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/controls/OrbitControls.js
                       sha256 80efaadea4f8a636a65fb0bd08bfef62f3d93a0bb94e2e7500f23176c5c07f4e
  LICENSE              https://cdn.jsdelivr.net/npm/three@0.170.0/LICENSE
                       sha256 4c40a1ef62450b857c3b2aaf294936304cd552d965fbcd9d32d4c5bcf4ba4454

STLLoader and OrbitControls import the bare name 'three'; web/templates/assets/stl_viewer.html maps it
to three.module.min.js with a nonce'd import map (CSP script-src 'self' + nonce, no CDN). Used by
web/static/js/stl-viewer.js. r170 is the last release before three.module.js was split into
three.module.js + three.core.js, so one file is the whole library.

Re-fetch / verify: scripts/vendor_three.sh (checks every sha256 above). If these files are missing,
the object page keeps the rendered thumbnail and says the 3D viewer isn't installed.
