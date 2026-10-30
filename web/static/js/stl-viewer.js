// #742: the STL object page's in-page 3D viewer (three.js + STLLoader + OrbitControls, vendored under
// /static/vendor/three/, mapped by the import map in templates/assets/stl_viewer.html).
//
// The page draws the rendered thumbnail inside #stl-viewer; this swaps it for a live view. It never
// fails silently: every reason the 3D view doesn't appear (file over the cap, no WebGL, vendored files
// missing, the download or the parse failing) is said in #stl-viewer-note, and the thumbnail stays.
// data-size / data-max / data-autoload are bytes: files over data-max never load in the browser;
// files over data-autoload wait for a "View in 3D" click.

const AZIMUTH = -45 * Math.PI / 180;   // the same 3/4 view as the server-rendered thumbnail
const ELEVATION = 28 * Math.PI / 180;
const FOV = 30;

function fmtMB(bytes) {
  return (bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0) + ' MB';
}

function hasWebGL() {
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch (e) {
    return false;
  }
}

function setup(root) {
  const note = root.querySelector('#stl-viewer-note');
  const btn = root.querySelector('#stl-viewer-load');
  const say = (msg) => { if (note) note.textContent = msg; };
  const size = parseInt(root.dataset.size || '', 10);
  const max = parseInt(root.dataset.max || '0', 10);
  const auto = parseInt(root.dataset.autoload || '0', 10);
  const known = Number.isFinite(size);

  if (known && max > 0 && size > max) {
    say('This file is ' + fmtMB(size) + ', over the 3D viewer\'s ' + fmtMB(max) +
        ' limit, so the page shows the rendered picture. Download it to turn it around in a slicer.');
    return;
  }
  if (!hasWebGL()) {
    say('This browser has WebGL turned off or missing, so the 3D viewer can\'t run; showing the rendered picture.');
    return;
  }
  const start = () => { if (btn) btn.hidden = true; load(root, say, max); };
  if (!known || size > auto) {
    if (btn) {
      btn.textContent = 'View in 3D' + (known ? ' (' + fmtMB(size) + ')' : '');
      btn.hidden = false;
      btn.addEventListener('click', start);
    }
    return;
  }
  start();
}

async function load(root, say, max) {
  say('Loading the 3D view…');
  let THREE, STLLoader, OrbitControls;
  try {
    THREE = await import('three');
    ({ STLLoader } = await import('three/stl-loader'));
    ({ OrbitControls } = await import('three/orbit-controls'));
  } catch (err) {
    console.error('STL viewer: three.js failed to load', err);
    say('The 3D viewer isn\'t installed on this server (the files in /static/vendor/three/ are missing; ' +
        'scripts/vendor_three.sh fetches them), so the page shows the rendered picture.');
    return;
  }

  let buffer;
  try {
    const res = await fetch(root.dataset.src, { credentials: 'same-origin' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    buffer = await res.arrayBuffer();
  } catch (err) {
    say('Couldn\'t download the file for the 3D view (' + err.message + '); showing the rendered picture.');
    return;
  }
  if (max > 0 && buffer.byteLength > max) {
    say('This file is ' + fmtMB(buffer.byteLength) + ', over the 3D viewer\'s ' + fmtMB(max) +
        ' limit; showing the rendered picture.');
    return;
  }

  let geometry;
  try {
    geometry = new STLLoader().parse(buffer);
    buffer = null;
    if (!geometry.attributes.position || geometry.attributes.position.count === 0) throw new Error('no triangles');
    geometry.computeBoundingBox();
    geometry.computeBoundingSphere();
    if (!(geometry.boundingSphere.radius > 0)) throw new Error('the mesh has no size');
  } catch (err) {
    say('The browser couldn\'t read this STL (' + err.message + '); showing the rendered picture.');
    return;
  }

  const stage = root.querySelector('#stl-viewer-stage');
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true });
  } catch (err) {
    say('The browser refused a 3D canvas (' + err.message + '); showing the rendered picture.');
    return;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  stage.hidden = false;
  stage.appendChild(renderer.domElement);
  const img = root.querySelector('#preview-img');
  if (img) img.style.display = 'none';  // the 3D view replaces the picture (the original link stays)

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x14170f);
  const material = new THREE.MeshStandardMaterial({ color: 0x9ea885, roughness: 0.6, metalness: 0.05, side: THREE.DoubleSide });
  scene.add(new THREE.Mesh(geometry, material));

  const center = geometry.boundingBox.getCenter(new THREE.Vector3());
  const radius = geometry.boundingSphere.radius;
  const camera = new THREE.PerspectiveCamera(FOV, 1, radius / 100, radius * 100);
  camera.up.set(0, 0, 1);  // STL is Z-up
  const dist = radius / Math.sin((FOV / 2) * Math.PI / 180) * 1.05;
  camera.position.set(
    center.x + dist * Math.cos(ELEVATION) * Math.cos(AZIMUTH),
    center.y + dist * Math.cos(ELEVATION) * Math.sin(AZIMUTH),
    center.z + dist * Math.sin(ELEVATION));
  scene.add(new THREE.HemisphereLight(0xffffff, 0x2a2d22, 0.9));
  const key = new THREE.DirectionalLight(0xffffff, 2.2);
  key.position.set(-0.6, 0.8, 1);  // in the camera's frame: it follows the view as it turns
  camera.add(key);
  scene.add(camera);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.copy(center);
  controls.update();

  const render = () => renderer.render(scene, camera);
  const resize = () => {
    const w = stage.clientWidth || 400;
    const h = stage.clientHeight || w;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    render();
  };
  controls.addEventListener('change', render);
  if (window.ResizeObserver) new ResizeObserver(resize).observe(stage);
  resize();
  say('Drag to turn, scroll to zoom, right-drag to pan.');
}

const root = document.getElementById('stl-viewer');
if (root) setup(root);
