/* ==========================================================
   Fundo do login — mosaico de câmeras com caixas de detecção,
   no mesmo arranjo do video wall da sala de monitoramento.

   A convenção visual é a do mosaico ao vivo (feeds.js): retângulo
   na cor da classe e, acima dele, uma barra sólida da mesma cor
   com o rótulo em texto escuro.

   Exposto como window.AuthFX3D; quem liga e desliga é o app.js.
   ========================================================== */
(function () {
  const AZUL = 0x35a7de;

  const COLS = 4, LINHAS = 3;
  const LARG = 16, ALT = 9;          // proporção do quadro, 16:9
  const VAO = 2.4;                   // vão entre quadros
  const N_CAIXAS = 8;                // detecções simultâneas no mosaico
  const ALT_ROTULO = 1.15;           // altura da barra do rótulo, em unidades

  // As mesmas classes e cores do mosaico real. Só as que o parque produz.
  const CLASSES = [
    { rotulo: 'PESSOA',      cor: '#35A7DE', peso: 34 },
    { rotulo: 'ROSTO',       cor: '#E05F9E', peso: 16 },
    { rotulo: 'CARRO',       cor: '#2FB67C', peso: 20 },
    { rotulo: 'MOTOCICLETA', cor: '#F0A02A', peso: 14 },
    { rotulo: 'BICICLETA',   cor: '#8C6FE0', peso: 9 },
    { rotulo: 'MOCHILA',     cor: '#E05F9E', peso: 7 }
  ];
  const PESO_TOTAL = CLASSES.reduce((s, c) => s + c.peso, 0);

  let renderer, scene, camera, grupo, caixas = [], painel = [];
  let raf = null, t = 0, ultimo = 0;
  let medidor = null;

  const aleatorio = (a, b) => a + Math.random() * (b - a);

  function sortearClasse() {
    let n = Math.random() * PESO_TOTAL;
    for (const c of CLASSES) { if ((n -= c.peso) <= 0) return c; }
    return CLASSES[0];
  }

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

  /** Barra sólida com o texto vazado em escuro, como a etiqueta do mosaico. */
  function texturaRotulo(texto, cor) {
    const escala = 3;                       // desenha grande e reduz, para não serrilhar
    const fonte = `600 ${11 * escala}px "IBM Plex Sans", system-ui, sans-serif`;
    if (!medidor) medidor = document.createElement('canvas').getContext('2d');
    medidor.font = fonte;

    const pad = 5 * escala;
    const w = Math.ceil(medidor.measureText(texto).width) + pad * 2;
    const h = 16 * escala;

    const cv = document.createElement('canvas');
    cv.width = w; cv.height = h;
    const ctx = cv.getContext('2d');
    ctx.fillStyle = cor;
    ctx.fillRect(0, 0, w, h);
    ctx.font = fonte;                       // definido após o resize, que zera o estado
    ctx.fillStyle = '#08121A';
    ctx.textBaseline = 'middle';
    ctx.fillText(texto, pad, h / 2 + 1);

    const tex = new THREE.CanvasTexture(cv);
    tex.minFilter = THREE.LinearFilter;
    tex.magFilter = THREE.LinearFilter;
    return { tex, proporcao: w / h };
  }

  /** Sorteia classe, tamanho e a trajetória que a caixa percorre no quadro. */
  function reposicionar(c) {
    const q = painel[Math.floor(Math.random() * painel.length)];
    const classe = sortearClasse();
    const w = aleatorio(2.4, 5.2);
    const h = aleatorio(2.8, 5.0);

    c.caixa.geometry.dispose();
    c.caixa.geometry = retangulo(w, h);
    c.caixa.material.color.set(classe.cor);

    // Confiança acima do limiar do sistema: 0,65 para objeto, 0,55 para face.
    const conf = Math.round(aleatorio(classe.rotulo === 'ROSTO' ? 58 : 67, 97));
    const { tex, proporcao } = texturaRotulo(`${classe.rotulo} ${conf}%`, classe.cor);
    if (c.rotulo.material.map) c.rotulo.material.map.dispose();
    c.rotulo.material.map = tex;
    c.rotulo.material.needsUpdate = true;
    c.rotulo.scale.set(ALT_ROTULO * proporcao, ALT_ROTULO, 1);

    // O rótulo encosta no canto superior esquerdo da caixa, como no mosaico.
    c.rotulo.position.set(
      -w / 2 + (ALT_ROTULO * proporcao) / 2,
      h / 2 + ALT_ROTULO / 2,
      0.02
    );

    // A caixa atravessa parte do quadro durante a vida: é um track, não um
    // carimbo. O deslocamento fica curto para não sair do próprio quadro.
    const limX = Math.max(0, (LARG - w) / 2 - 0.4);
    const limY = Math.max(0, (ALT - h) / 2 - ALT_ROTULO - 0.2);
    c.de = { x: q.x + aleatorio(-limX, limX), y: q.y + aleatorio(-limY, limY) };
    const passo = aleatorio(0.8, 2.6);
    const ang = Math.random() * Math.PI * 2;
    c.para = {
      x: Math.min(q.x + limX, Math.max(q.x - limX, c.de.x + Math.cos(ang) * passo)),
      y: Math.min(q.y + limY, Math.max(q.y - limY, c.de.y + Math.sin(ang) * passo))
    };

    c.t = 0;
    c.dur = aleatorio(2.2, 4.6);
    c.espera = aleatorio(0.2, 2.6);
    c.grupo.visible = false;
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

    painel = [];
    for (let l = 0; l < LINHAS; l++) {
      for (let c = 0; c < COLS; c++) {
        painel.push({
          x: (c - (COLS - 1) / 2) * (LARG + VAO),
          y: ((LINHAS - 1) / 2 - l) * (ALT + VAO),
          n: l * COLS + c + 1
        });
      }
    }

    // Fundo, moldura e identificação de cada quadro. Nada aqui muda depois.
    painel.forEach(q => {
      const fundo = new THREE.Mesh(
        new THREE.PlaneGeometry(LARG, ALT),
        new THREE.MeshBasicMaterial({
          color: 0x0b131c, transparent: true,
          opacity: aleatorio(0.5, 0.82), depthWrite: false
        })
      );
      fundo.position.set(q.x, q.y, 0);
      grupo.add(fundo);

      const moldura = new THREE.LineSegments(
        retangulo(LARG, ALT),
        new THREE.LineBasicMaterial({
          color: AZUL, transparent: true, opacity: 0.2, depthWrite: false
        })
      );
      moldura.position.set(q.x, q.y, 0.05);
      grupo.add(moldura);

      // Sem o código da câmera o quadro fica um retângulo vazio; é ele que diz
      // que aquilo é um posto de observação.
      const cod = `CAM-${String(q.n).padStart(3, '0')}`;
      const { tex, proporcao } = texturaRotulo(cod, 'rgba(11,19,28,0)');
      const etiq = new THREE.Sprite(new THREE.SpriteMaterial({
        map: tex, transparent: true, opacity: 0.42, depthWrite: false
      }));
      etiq.scale.set(ALT_ROTULO * proporcao, ALT_ROTULO, 1);
      etiq.position.set(q.x - LARG / 2 + (ALT_ROTULO * proporcao) / 2 + 0.3,
                        q.y + ALT / 2 - ALT_ROTULO / 2 - 0.3, 0.06);
      grupo.add(etiq);
    });

    caixas = Array.from({ length: N_CAIXAS }, () => {
      const g = new THREE.Group();
      const caixa = new THREE.LineSegments(
        retangulo(3, 3),
        new THREE.LineBasicMaterial({
          color: AZUL, transparent: true, opacity: 0, depthWrite: false
        })
      );
      const rotulo = new THREE.Sprite(new THREE.SpriteMaterial({
        transparent: true, opacity: 0, depthWrite: false
      }));
      g.add(caixa); g.add(rotulo);
      g.position.z = 0.1;
      grupo.add(g);

      const c = { grupo: g, caixa, rotulo, t: 0, dur: 1, espera: 0, de: null, para: null };
      reposicionar(c);
      c.espera = aleatorio(0, 4);   // entradas espalhadas no tempo
      return c;
    });

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
    if (k < 0.1) return k / 0.1;
    if (k > 0.74) return Math.max(0, (1 - k) / 0.26);
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
      c.grupo.visible = true;
      c.t += dt;
      const k = c.t / c.dur;
      if (k >= 1) { reposicionar(c); return; }

      const o = envelope(k);
      c.caixa.material.opacity = o * 0.9;
      c.rotulo.material.opacity = o * 0.92;
      c.grupo.position.x = c.de.x + (c.para.x - c.de.x) * k;
      c.grupo.position.y = c.de.y + (c.para.y - c.de.y) * k;
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
