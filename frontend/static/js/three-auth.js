/* ==========================================================
   Fundo do login — malha de nós conectados girando em WebGL.
   Exposto como window.AuthFX3D; quem liga e desliga é o app.js.
   ========================================================== */
(function () {
  const AZUL = 0x35a7de;
  const AMBAR = 0xf0a02a;
  const N_NOS = 150;
  const RAIO = 26;
  const DIST_LIGACAO = 9.5;

  let renderer, scene, camera, grupo, nos, ligacoes, varredura;
  let raf = null;
  let pulsos = [];
  let corBase, corPulso;
  let t = 0;

  function esferaFibonacci(n, raio) {
    // Distribuição uniforme: o espiral de Fibonacci evita o acúmulo nos polos
    // que uma amostragem ingênua de lat/long produz.
    const pts = [];
    const phi = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < n; i++) {
      const y = 1 - (i / (n - 1)) * 2;
      const r = Math.sqrt(1 - y * y);
      const th = phi * i;
      pts.push(new THREE.Vector3(Math.cos(th) * r * raio, y * raio, Math.sin(th) * r * raio));
    }
    return pts;
  }

  function construir(canvas) {
    scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x05080c, 0.016);

    camera = new THREE.PerspectiveCamera(52, 1, 0.1, 400);
    camera.position.set(0, 0, 74);

    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setClearColor(0x05080c, 1);

    grupo = new THREE.Group();
    scene.add(grupo);

    const pts = esferaFibonacci(N_NOS, RAIO);
    corBase = new THREE.Color(AZUL);
    corPulso = new THREE.Color(AMBAR);

    // --- nós ---
    const posNos = new Float32Array(N_NOS * 3);
    const corNos = new Float32Array(N_NOS * 3);
    pts.forEach((p, i) => {
      posNos[i * 3] = p.x; posNos[i * 3 + 1] = p.y; posNos[i * 3 + 2] = p.z;
      corNos[i * 3] = corBase.r; corNos[i * 3 + 1] = corBase.g; corNos[i * 3 + 2] = corBase.b;
    });
    const gNos = new THREE.BufferGeometry();
    gNos.setAttribute('position', new THREE.BufferAttribute(posNos, 3));
    gNos.setAttribute('color', new THREE.BufferAttribute(corNos, 3));
    nos = new THREE.Points(gNos, new THREE.PointsMaterial({
      size: 1.5, vertexColors: true, transparent: true, opacity: 0.95,
      sizeAttenuation: true, depthWrite: false
    }));
    grupo.add(nos);

    // --- ligações (topologia fixa: calculada uma vez, só o grupo gira) ---
    const seg = [];
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        if (pts[i].distanceTo(pts[j]) < DIST_LIGACAO) {
          seg.push(pts[i].x, pts[i].y, pts[i].z, pts[j].x, pts[j].y, pts[j].z);
        }
      }
    }
    const gLig = new THREE.BufferGeometry();
    gLig.setAttribute('position', new THREE.BufferAttribute(new Float32Array(seg), 3));
    ligacoes = new THREE.LineSegments(gLig, new THREE.LineBasicMaterial({
      color: AZUL, transparent: true, opacity: 0.16, depthWrite: false
    }));
    grupo.add(ligacoes);

    // --- anel de varredura ---
    varredura = new THREE.Mesh(
      new THREE.RingGeometry(RAIO * 1.28, RAIO * 1.32, 96),
      new THREE.MeshBasicMaterial({
        color: AZUL, transparent: true, opacity: 0.2,
        side: THREE.DoubleSide, depthWrite: false
      })
    );
    varredura.rotation.x = Math.PI / 2.35;
    grupo.add(varredura);

    // Nós que acendem em âmbar, como uma identificação sendo registrada.
    pulsos = Array.from({ length: 7 }, () => ({
      i: Math.floor(Math.random() * N_NOS),
      fase: Math.random() * Math.PI * 2,
      vel: 0.5 + Math.random() * 0.7
    }));

    redimensionar();
  }

  function redimensionar() {
    if (!renderer) return;
    const w = window.innerWidth, h = window.innerHeight;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(w, h, false);
  }

  function quadro() {
    t += 0.005;

    grupo.rotation.y += 0.0016;
    grupo.rotation.x = Math.sin(t * 0.4) * 0.16;

    varredura.rotation.z += 0.004;
    varredura.material.opacity = 0.12 + (Math.sin(t * 1.6) + 1) * 0.06;
    ligacoes.material.opacity = 0.11 + (Math.sin(t * 0.9) + 1) * 0.035;

    const cores = nos.geometry.attributes.color;
    pulsos.forEach(p => {
      const k = (Math.sin(t * p.vel * 3 + p.fase) + 1) / 2;
      cores.setXYZ(p.i,
        corBase.r + (corPulso.r - corBase.r) * k,
        corBase.g + (corPulso.g - corBase.g) * k,
        corBase.b + (corPulso.b - corBase.b) * k);
      if (k < 0.02) p.i = Math.floor(Math.random() * N_NOS);
    });
    cores.needsUpdate = true;

    renderer.render(scene, camera);
    raf = requestAnimationFrame(quadro);
  }

  window.AuthFX3D = {
    iniciar() {
      if (typeof THREE === 'undefined') return;
      const canvas = document.getElementById('authCanvas');
      if (!canvas || raf !== null) return;
      try {
        if (!renderer) construir(canvas);
      } catch (e) {
        return;  // sem WebGL a tela segue utilizável, só sem o fundo
      }
      window.addEventListener('resize', redimensionar);
      quadro();
    },
    parar() {
      if (raf !== null) { cancelAnimationFrame(raf); raf = null; }
      window.removeEventListener('resize', redimensionar);
    }
  };
})();
