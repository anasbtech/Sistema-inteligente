/* ==========================================================
   Fundo do login — mosaico de câmeras com caixas de detecção,
   no mesmo arranjo do video wall da sala de monitoramento.
   Exposto como window.AuthFX3D; quem liga e desliga é o app.js.
   ========================================================== */
(function () {
  const AZUL = 0x35a7de;
  const AMBAR = 0xf0a02a;

  const COLS = 4, LINHAS = 3;
  const LARG = 16, ALT = 9;          // proporção do quadro, 16:9
  const VAO = 2.4;                   // vão entre quadros
  const N_CAIXAS = 9;                // detecções simultâneas no mosaico

  let renderer, scene, camera, grupo, caixas = [];
  let raf = null, t = 0, ultimo = 0;

  const aleatorio = (a, b) => a + Math.random() * (b - a);

  function retangulo(w, h) {
    const x = w / 2, y = h / 2;
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array([
      -x, -y, 0, x, -y, 0,
      x, -y, 0, x, y, 0,
      x, y, 0, -x, y, 0,
      -x, y, 0, -x, -y, 0
    ]), 3));
    return g;
  }

  /** Posição do canto inferior-esquerdo de cada quadro do mosaico. */
  function quadros() {
    const lista = [];
    for (let l = 0; l < LINHAS; l++) {
      for (let c = 0; c < COLS; c++) {
        lista.push({
          x: (c - (COLS - 1) / 2) * (LARG + VAO),
          y: ((LINHAS - 1) / 2 - l) * (ALT + VAO)
        });
      }
    }
    return lista;
  }

  /** Sorteia onde e por quanto tempo uma caixa aparece. */
  function reposicionar(caixa, painel) {
    const p = painel[Math.floor(Math.random() * painel.length)];
    const w = aleatorio(2.2, 5.4);
    const h = aleatorio(2.6, 5.2);
    caixa.malha.geometry.dispose();
    caixa.malha.geometry = retangulo(w, h);
    caixa.malha.position.set(
      p.x + aleatorio(-(LARG - w) / 2, (LARG - w) / 2),
      p.y + aleatorio(-(ALT - h) / 2, (ALT - h) / 2),
      0.1
    );
    // Âmbar é a captura facial, azul é objeto — a mesma convenção do mosaico.
    caixa.malha.material.color.setHex(Math.random() < 0.3 ? AMBAR : AZUL);
    caixa.t = 0;
    caixa.dur = aleatorio(1.6, 4.2);
    caixa.espera = aleatorio(0, 2.4);
  }

  function construir(canvas) {
    scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x05080c, 0.0075);

    camera = new THREE.PerspectiveCamera(50, 1, 0.1, 400);
    camera.position.set(0, 0, 62);

    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setClearColor(0x05080c, 1);

    grupo = new THREE.Group();
    grupo.rotation.y = -0.16;
    grupo.rotation.x = 0.05;
    scene.add(grupo);

    const painel = quadros();

    // Fundo e moldura de cada quadro. A topologia é fixa: nada aqui muda
    // depois, só as caixas por cima e a inclinação do grupo.
    painel.forEach(p => {
      const fundo = new THREE.Mesh(
        new THREE.PlaneGeometry(LARG, ALT),
        new THREE.MeshBasicMaterial({
          color: 0x0b131c, transparent: true,
          opacity: aleatorio(0.5, 0.82), depthWrite: false
        })
      );
      fundo.position.set(p.x, p.y, 0);
      grupo.add(fundo);

      const moldura = new THREE.LineSegments(
        retangulo(LARG, ALT),
        new THREE.LineBasicMaterial({
          color: AZUL, transparent: true, opacity: 0.2, depthWrite: false
        })
      );
      moldura.position.set(p.x, p.y, 0.05);
      grupo.add(moldura);
    });

    caixas = Array.from({ length: N_CAIXAS }, () => {
      const malha = new THREE.LineSegments(
        retangulo(3, 3),
        new THREE.LineBasicMaterial({
          color: AZUL, transparent: true, opacity: 0, depthWrite: false
        })
      );
      grupo.add(malha);
      const caixa = { malha, t: 0, dur: 1, espera: 0 };
      reposicionar(caixa, painel);
      caixa.espera = aleatorio(0, 3.5);   // entradas espalhadas no tempo
      return caixa;
    });

    grupo.userData.painel = painel;
    redimensionar();
  }

  function redimensionar() {
    if (!renderer) return;
    const canvas = renderer.domElement;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    // A coluna da marca some abaixo de 760px; sem quadro válido não há o que
    // dimensionar, e um aspect de 0 quebraria a matriz de projeção.
    if (w < 2 || h < 2) return;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(w, h, false);
  }

  /** Entra depressa, permanece, sai devagar — como uma detecção confirmada. */
  function envelope(k) {
    if (k < 0.12) return k / 0.12;
    if (k > 0.72) return Math.max(0, (1 - k) / 0.28);
    return 1;
  }

  function quadro(agora) {
    const dt = Math.min((agora - ultimo) / 1000 || 0, 0.05);
    ultimo = agora;
    t += dt;

    // Respiração lenta do mosaico, para o fundo não ficar estático.
    grupo.rotation.y = -0.16 + Math.sin(t * 0.17) * 0.05;
    grupo.rotation.x = 0.05 + Math.cos(t * 0.13) * 0.025;

    caixas.forEach(c => {
      if (c.espera > 0) { c.espera -= dt; return; }
      c.t += dt;
      const k = c.t / c.dur;
      if (k >= 1) { reposicionar(c, grupo.userData.painel); return; }
      c.malha.material.opacity = envelope(k) * 0.85;
    });

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
      redimensionar();
      ultimo = performance.now();
      raf = requestAnimationFrame(quadro);
    },
    parar() {
      if (raf !== null) { cancelAnimationFrame(raf); raf = null; }
      window.removeEventListener('resize', redimensionar);
    }
  };
})();
