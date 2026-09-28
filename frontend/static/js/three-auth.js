// Three.js animated background for login screen
// Secure intelligence theme with geometric patterns

let scene, camera, renderer;
let particles = [];
let securityShapes = [];

function initThreeAuth() {
  const canvas = document.getElementById('authCanvas');
  if (!canvas) return;

  // Scene setup
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0a0e14);
  scene.fog = new THREE.Fog(0x0a0e14, 100, 500);

  // Camera
  camera = new THREE.PerspectiveCamera(60, canvas.clientWidth / canvas.clientHeight, 0.1, 1000);
  camera.position.z = 50;

  // Renderer
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setSize(canvas.clientWidth, canvas.clientHeight);
  renderer.setPixelRatio(window.devicePixelRatio);

  // Lighting
  const ambientLight = new THREE.AmbientLight(0xffffff, 0.4);
  scene.add(ambientLight);

  const pointLight = new THREE.PointLight(0x35a7de, 1, 100);
  pointLight.position.set(30, 30, 30);
  scene.add(pointLight);

  // Create particle system
  createParticles();

  // Create security-themed geometric shapes
  createSecurityShapes();

  // Animation loop
  animate();

  // Handle window resize
  window.addEventListener('resize', onWindowResize);
}

function createParticles() {
  const geometry = new THREE.BufferGeometry();
  const count = 200;
  const posArray = new Float32Array(count * 3);

  for (let i = 0; i < count * 3; i += 3) {
    posArray[i] = (Math.random() - 0.5) * 200;
    posArray[i + 1] = (Math.random() - 0.5) * 200;
    posArray[i + 2] = (Math.random() - 0.5) * 200;
  }

  geometry.setAttribute('position', new THREE.BufferAttribute(posArray, 3));

  const material = new THREE.PointsMaterial({
    size: 0.5,
    color: 0x35a7de,
    sizeAttenuation: true,
    transparent: true,
    opacity: 0.6
  });

  const points = new THREE.Points(geometry, material);
  scene.add(points);

  particles.push({
    mesh: points,
    originalPositions: posArray.slice(),
    velocities: Array(count).fill(0).map(() => ({
      x: (Math.random() - 0.5) * 0.2,
      y: (Math.random() - 0.5) * 0.2,
      z: (Math.random() - 0.5) * 0.2
    }))
  });
}

function createSecurityShapes() {
  // Central rotating cube
  const cubeGeometry = new THREE.BoxGeometry(8, 8, 8);
  const cubeMaterial = new THREE.MeshPhongMaterial({
    color: 0x35a7de,
    emissive: 0x35a7de,
    emissiveIntensity: 0.3,
    wireframe: true
  });
  const cube = new THREE.Mesh(cubeGeometry, cubeMaterial);
  cube.position.z = -10;
  scene.add(cube);
  securityShapes.push({ mesh: cube, type: 'rotate' });

  // Dodecahedron (12-sided shape) - security/shield theme
  const dodecGeometry = new THREE.DodecahedronGeometry(6, 0);
  const dodecMaterial = new THREE.MeshPhongMaterial({
    color: 0xf0a02a,
    emissive: 0xf0a02a,
    emissiveIntensity: 0.2,
    wireframe: true,
    transparent: true,
    opacity: 0.3
  });
  const dodecahedron = new THREE.Mesh(dodecGeometry, dodecMaterial);
  dodecahedron.position.set(20, -15, -20);
  dodecahedron.rotation.x = Math.random() * Math.PI * 2;
  scene.add(dodecahedron);
  securityShapes.push({ mesh: dodecahedron, type: 'float' });

  // Icosahedron (20-sided shape)
  const icoGeometry = new THREE.IcosahedronGeometry(5, 1);
  const icoMaterial = new THREE.MeshPhongMaterial({
    color: 0x35a7de,
    emissive: 0x35a7de,
    emissiveIntensity: 0.15,
    wireframe: true,
    transparent: true,
    opacity: 0.2
  });
  const icosahedron = new THREE.Mesh(icoGeometry, icoMaterial);
  icosahedron.position.set(-25, 15, -25);
  icosahedron.rotation.z = Math.random() * Math.PI * 2;
  scene.add(icosahedron);
  securityShapes.push({ mesh: icosahedron, type: 'float' });

  // Tetrahedron (4-sided shape)
  const tetGeometry = new THREE.TetrahedronGeometry(7, 1);
  const tetMaterial = new THREE.MeshPhongMaterial({
    color: 0xf0a02a,
    emissive: 0xf0a02a,
    emissiveIntensity: 0.1,
    wireframe: true,
    transparent: true,
    opacity: 0.15
  });
  const tetrahedron = new THREE.Mesh(tetGeometry, tetMaterial);
  tetrahedron.position.set(-10, -20, -15);
  scene.add(tetrahedron);
  securityShapes.push({ mesh: tetrahedron, type: 'float' });
}

function animate() {
  requestAnimationFrame(animate);

  // Update particles
  particles.forEach(particle => {
    const positions = particle.mesh.geometry.attributes.position.array;

    for (let i = 0; i < positions.length; i += 3) {
      positions[i] += particle.velocities[i / 3].x;
      positions[i + 1] += particle.velocities[i / 3].y;
      positions[i + 2] += particle.velocities[i / 3].z;

      // Boundary wrapping
      if (positions[i] > 100) positions[i] = -100;
      if (positions[i] < -100) positions[i] = 100;
      if (positions[i + 1] > 100) positions[i + 1] = -100;
      if (positions[i + 1] < -100) positions[i + 1] = 100;
    }

    particle.mesh.geometry.attributes.position.needsUpdate = true;
  });

  // Animate shapes
  securityShapes.forEach((shape, idx) => {
    if (shape.type === 'rotate') {
      shape.mesh.rotation.x += 0.002;
      shape.mesh.rotation.y += 0.003;
      shape.mesh.rotation.z += 0.001;
    } else if (shape.type === 'float') {
      shape.mesh.rotation.x += 0.001;
      shape.mesh.rotation.y += 0.002;
      shape.mesh.rotation.z += 0.0015;
      shape.mesh.position.y += Math.sin(Date.now() * 0.0003 + idx) * 0.02;
    }
  });

  renderer.render(scene, camera);
}

function onWindowResize() {
  const canvas = document.getElementById('authCanvas');
  if (!canvas) return;

  const width = canvas.clientWidth;
  const height = canvas.clientHeight;

  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  renderer.setSize(width, height);
}

// Initialize when DOM is ready and Three.js is loaded
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    setTimeout(() => {
      if (typeof THREE !== 'undefined') {
        initThreeAuth();
      }
    }, 50);
  });
} else {
  setTimeout(() => {
    if (typeof THREE !== 'undefined') {
      initThreeAuth();
    }
  }, 50);
}
