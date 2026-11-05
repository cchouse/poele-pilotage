/* Carte « Pilotage poêle » — chargée automatiquement par l'intégration.
 * Répond à : que fait le poêle, quand sera-t-il allumé et POURQUOI, qu'ai-je appris, et que puis-je forcer ou annuler. */
const VERSION = "0.1.0";
const fmt = (x, n = 1) => (x === null || x === undefined || Number.isNaN(x)) ? "–" : Number(x).toFixed(n).replace(".", ",");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
const jjmm = (ts) => new Date(ts * 1000).toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
const ETATS = ["arret", "allumage", "marche", "extinction", "probleme", "inconnu"];
const ICONE = { arret: "○", allumage: "◐", marche: "●", extinction: "◑", probleme: "⚠", inconnu: "?" };
const SOURCES = { auto: "automatique", force: "forcé", manuel: "manuel" };

const STYLE = `
:host { display:block; } ha-card { padding:16px; } * { box-sizing:border-box; }
.tete { display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:10px; }
.titre { font-size:1.1rem; font-weight:600; color:var(--primary-text-color); }
.puce { font-size:.9rem; font-weight:600; padding:6px 12px; border-radius:999px; border:2px solid var(--primary-color); color:var(--primary-text-color); white-space:nowrap; }
.puce.pb { border-color:var(--error-color,#b3261e); }
.bandeau { border:2px solid var(--divider-color); border-radius:12px; padding:12px; }
.bandeau.act { border-color:var(--primary-color); }
.heure { font-size:2.1rem; font-weight:600; line-height:1.1; color:var(--primary-text-color); }
.sous { font-size:.9rem; color:var(--secondary-text-color); line-height:1.45; margin-top:6px; }
.pourquoi { font-size:.92rem; color:var(--primary-text-color); line-height:1.5; margin-top:8px; }
.mode { display:inline-block; margin-top:8px; font-size:.8rem; font-weight:600; padding:4px 10px; border:1px solid var(--divider-color); border-radius:999px; color:var(--secondary-text-color); }
.actions { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:12px; }
button { font:inherit; color:var(--primary-text-color); cursor:pointer; }
button.b { min-height:52px; border-radius:12px; border:2px solid var(--primary-color); background:transparent; font-weight:600; padding:0 10px; }
button.b:hover { background:color-mix(in srgb, var(--primary-color) 12%, transparent); }
button.b:disabled { opacity:.5; cursor:not-allowed; }
button:focus-visible { outline:3px solid var(--primary-color); outline-offset:2px; }
.inter { display:grid; grid-template-columns:1fr; gap:8px; margin-top:12px; }
button.t { min-height:48px; display:flex; justify-content:space-between; align-items:center; padding:0 14px; border-radius:12px; border:2px solid var(--divider-color); background:transparent; font-weight:600; text-align:left; }
button.t[aria-pressed="true"] { border-color:var(--primary-color); background:color-mix(in srgb, var(--primary-color) 14%, transparent); }
.sec { margin-top:18px; } .sec h3 { margin:0 0 6px; font-size:.95rem; font-weight:600; color:var(--primary-text-color); }
.petit { font-size:.8rem; color:var(--secondary-text-color); line-height:1.4; }
svg.g { width:100%; height:auto; display:block; }
.lg { display:flex; flex-wrap:wrap; gap:12px; font-size:.75rem; color:var(--secondary-text-color); margin-top:4px; }
.lg i { display:inline-block; width:16px; height:10px; margin-right:4px; vertical-align:middle; border-radius:2px; }
.piece { padding:10px; border:2px solid transparent; border-radius:10px; font-size:.9rem; color:var(--primary-text-color); }
.piece.lim { border-color:var(--primary-color); }
.piece .l1 { display:flex; justify-content:space-between; gap:8px; font-weight:600; }
.piece .l2 { color:var(--secondary-text-color); font-size:.8rem; margin-top:2px; }
ul { list-style:none; margin:0; padding:0; } li { padding:6px 0; border-bottom:1px solid var(--divider-color); font-size:.88rem; color:var(--primary-text-color); } li:last-child { border:0; }
.vide { color:var(--secondary-text-color); font-size:.88rem; }
details { margin-top:14px; border:1px solid var(--divider-color); border-radius:12px; padding:0 12px; }
summary { min-height:48px; display:flex; align-items:center; cursor:pointer; font-weight:600; color:var(--primary-text-color); }
details p, details ul { margin:0 0 12px; font-size:.9rem; color:var(--primary-text-color); line-height:1.5; }
@media (forced-colors: active) { .puce,.bandeau.act,button.b,button.t[aria-pressed="true"],.piece.lim { border-color:Highlight; } }
@media (prefers-reduced-motion: reduce) { * { transition:none !important; animation:none !important; } }
`;

class PilotagePoeleCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) throw new Error("Choisissez l'entité « État du poêle ».");
    this._config = config; if (!this.shadowRoot) this.attachShadow({ mode: "open" }); this._cle = null;
  }
  set hass(h) {
    this._hass = h; const st = h.states[this._config.entity];
    const cle = st ? st.last_updated + "|" + st.state : "absent";
    if (cle === this._cle) return; this._cle = cle; this._dessiner(st);
  }
  getCardSize() { return 12; }
  static getConfigElement() { return document.createElement("pilotage-poele-card-editor"); }
  static getStubConfig(h) {
    const e = Object.keys(h.states).find((k) => k.startsWith("sensor.") && h.states[k].attributes && h.states[k].attributes.garde_fous && h.states[k].attributes.boutons);
    return { entity: e || "" };
  }
  _appel(domaine, service, eid) { if (eid) this._hass.callService(domaine, service, { entity_id: eid }); }

  _frise(a) {
    const h = a.historique || []; const now = Math.floor(Date.now() / 1000);
    const t0 = now - 12 * 3600, t1 = now + 12 * 3600, W = 340, pad = 8, Hs = 34;
    const X = (t) => pad + ((t - t0) / (t1 - t0)) * (W - 2 * pad);
    let barres = "";
    for (let i = 0; i < h.length; i++) {
      const [ts, e] = h[i]; const fin = i + 1 < h.length ? h[i + 1][0] : now;
      const nom = ETATS[e]; if (nom !== "marche" && nom !== "allumage" && nom !== "extinction") continue;
      const op = nom === "marche" ? 1 : 0.5;
      barres += `<rect x="${X(Math.max(ts, t0)).toFixed(1)}" y="8" width="${Math.max(1.5, X(fin) - X(Math.max(ts, t0))).toFixed(1)}" height="${Hs - 12}" fill="var(--primary-color)" opacity="${op}"/>`;
    }
    const p = a.plan; let prevu = "";
    if (p && p.besoin && p.allumage && p.echeance) {
      const x0 = X(Math.max(p.allumage, now)), x1 = X(p.echeance);
      if (x1 > x0) prevu = `<rect x="${x0.toFixed(1)}" y="8" width="${(x1 - x0).toFixed(1)}" height="${Hs - 12}" fill="none" stroke="var(--primary-color)" stroke-width="2" stroke-dasharray="4 3"/>`;
    }
    let axe = "";
    for (let k = -12; k <= 12; k += 3) { const t = now + k * 3600; const d = new Date(t * 1000);
      axe += `<text x="${X(t).toFixed(1)}" y="${Hs + 12}" font-size="9" text-anchor="middle" fill="var(--secondary-text-color)">${String(d.getHours()).padStart(2, "0")}h</text>`; }
    return `<svg class="g" viewBox="0 0 ${W} ${Hs + 16}" role="img" aria-label="Frise du poêle : marche passée et allumage prévu sur 24 heures">
      <rect x="${pad}" y="8" width="${W - 2 * pad}" height="${Hs - 12}" fill="var(--divider-color)" opacity=".5" rx="3"/>${barres}${prevu}
      <line x1="${X(now)}" x2="${X(now)}" y1="2" y2="${Hs - 2}" stroke="var(--primary-text-color)" stroke-width="2"/>${axe}</svg>
      <div class="lg"><span><i style="background:var(--primary-color)"></i>Marche</span><span><i style="border:2px dashed var(--primary-color)"></i>Chauffe prévue</span><span>| Maintenant</span></div>`;
  }

  _courbes(a, cle, titre, fn) {
    const h = a.historique || []; if (h.length < 2) return `<div class="vide">Les courbes apparaîtront après quelques mesures (un point toutes les 5 minutes).</div>`;
    const W = 340, H = 110, pad = 8; const series = fn(h); const vals = series.flatMap((s) => s.pts.map((p) => p[1])).filter((v) => v != null);
    if (!vals.length) return `<div class="vide">Pas de données pour ${esc(titre)}.</div>`;
    let lo = Math.min(...vals) - .3, hi = Math.max(...vals) + .3; if (hi - lo < 2) { const m = (hi + lo) / 2; lo = m - 1; hi = m + 1; }
    const ts = h.map((x) => x[0]); const a0 = ts[0], a1 = ts[ts.length - 1];
    const X = (t) => pad + ((t - a0) / Math.max(1, a1 - a0)) * (W - 2 * pad), Y = (v) => H - pad - ((v - lo) / (hi - lo)) * (H - 2 * pad);
    const tr = series.map((s, i) => { const d = s.pts.filter((p) => p[1] != null).map((p, k) => `${k ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ");
      return `<path d="${d}" fill="none" stroke="var(--primary-text-color)" stroke-width="2.2" stroke-dasharray="${["", "6 3", "2 3", "8 3 2 3"][i % 4]}"/>`; }).join("");
    const lg = series.map((s, i) => `<span>${["━", "╍", "┈", "╌"][i % 4]} ${esc(s.nom)}</span>`).join("");
    return `<svg class="g" viewBox="0 0 ${W} ${H}" role="img" aria-label="Courbe ${esc(titre)} sur 24 heures">${tr}
      <text x="${pad}" y="10" font-size="9" fill="var(--secondary-text-color)">${fmt(hi, 0)} °C</text><text x="${pad}" y="${H - 1}" font-size="9" fill="var(--secondary-text-color)">${fmt(lo, 0)} °C</text></svg><div class="lg">${lg}</div>`;
  }

  _dessiner(st) {
    const r = this.shadowRoot;
    if (!st) { r.innerHTML = `<style>${STYLE}</style><ha-card><p>Entité introuvable : ${esc(this._config.entity)}</p></ha-card>`; return; }
    const a = st.attributes || {}; const b = a.boutons || {}; const d = a.drapeaux || {}; const p = a.plan; const au = a.autorisation || {};
    const gf = a.garde_fous || {}; const ap = a.appris || {};
    const pb = a.etat === "probleme" || d.verrou;
    const actif = ["allumage", "marche"].includes(a.etat);
    // bandeau « prochain allumage »
    let ban;
    if (a.etat === "marche" || a.etat === "allumage") ban = `<div class="heure">${esc(a.etat_libelle)}</div><div class="sous">${a.run ? `Depuis ${hhmm(a.run.debut)} · allumage ${esc(SOURCES[a.run.source] || a.run.source)}` : ""}</div>`;
    else if (p && p.besoin && p.allumage) ban = `<div class="heure">${p.en_retard ? "Maintenant" : hhmm(p.allumage)}</div><div class="sous">Prochain allumage conseillé — objectif ${hhmm(p.echeance)}</div><div class="pourquoi">${esc(p.raison)}</div>`;
    else ban = `<div class="heure">Pas d'allumage prévu</div><div class="pourquoi">${esc(p ? p.raison : "Je n'ai pas encore de pièce à surveiller : ajoutez une pièce depuis la page de l'intégration.")}</div>`;
    const mode = d.auto ? "Automatique : j'allume seul si toutes les conditions sont réunies" : "Mode conseil : rien n'est envoyé au poêle";
    const piecesHtml = ((p && p.pieces) || []).map((x, i) => `<div class="piece ${i === 0 && p.besoin ? "lim" : ""}"><div class="l1"><span>${esc(x.nom)}${i === 0 && p.besoin ? " · la plus lente" : ""}</span><span>${fmt(x.temperature)} → ${fmt(x.cible)} °C</span></div>
       <div class="l2">${x.besoin > 0.05 ? `manque ${fmt(x.besoin)} °C` : "à la cible"} · monte de ${fmt(x.montee)} °C/h ${x.montee_apprise ? "(appris)" : "(par défaut, pas encore appris)"} · ≈ ${x.duree_min} min de chauffe</div></div>`).join("") || `<div class="vide">Aucune pièce configurée.</div>`;
    const piecesAppris = Object.entries(ap.pieces || {}).map(([n, v]) => `<li>${esc(n)} : ${v.appris ? `montée apprise sur ${v.n} chauffe${v.n > 1 ? "s" : ""} (${fmt(v.montee)} °C/h)` : "pas encore apprise (valeur par défaut)"}</li>`).join("");
    const journal = (a.journal || []).map((j) => `<li>${jjmm(j.debut)} · ${esc(SOURCES[j.source] || j.source)} · ${j.duree_min} min${j.allumage_min != null ? ` (allumage ${j.allumage_min} min)` : ""}${Object.keys(j.pieces || {}).length ? " · montée " + Object.entries(j.pieces).map(([n, v]) => `${esc(n)} ${fmt(v)} °C/h`).join(", ") : ""}</li>`).join("") || `<li class="vide">Aucun cycle enregistré pour l'instant.</li>`;
    const da = a.derniere_action;
    const inter = (cle, lib) => `<button class="t" id="${cle}" aria-pressed="${!!d[cle]}" ${b[cle] ? "" : "disabled"}><span>${lib}</span><span>${d[cle] ? "● Activé" : "○ Désactivé"}</span></button>`;
    r.innerHTML = `<style>${STYLE}</style><ha-card>
      <div class="tete"><span class="titre">Poêle à granulés</span><span class="puce ${pb ? "pb" : ""}"><span aria-hidden="true">${ICONE[a.etat] || "?"}</span> ${esc(a.etat_libelle)}</span></div>
      ${d.verrou ? `<div class="bandeau" role="alert" style="margin-bottom:10px;border-color:var(--error-color,#b3261e)"><b>⚠ Allumage automatique verrouillé</b> après un échec d'allumage. Vérifiez le poêle, puis réactivez l'allumage automatique.</div>` : ""}
      <div class="bandeau ${d.auto ? "act" : ""}">${ban}<span class="mode">${mode}</span>
        <div class="sous">${esc(au.ok ? "✓ " : "") + esc(au.raison || "")}</div></div>
      <div class="actions"><button class="b" id="allumer" ${b.allumer ? "" : "disabled"}>▶ Allumer maintenant</button>
        <button class="b" id="annuler" ${b.annuler ? "" : "disabled"}>${actif ? "■ Annuler / éteindre" : "✕ Annuler l'allumage prévu"}</button></div>
      <div class="inter">${inter("auto", "Allumage automatique")}${inter("arret_auto", "Arrêt automatique")}${inter("absent", "Mode absent")}</div>
      <div class="sec"><h3>Le poêle sur 24 heures</h3>${this._frise(a)}</div>
      <div class="sec"><h3>Les pièces (la plus lente décide)</h3>${piecesHtml}</div>
      <div class="sec"><h3>Températures des pièces</h3>${this._courbes(a, "p", "des pièces", (h) => (a.pieces_noms || []).map((n, i) => ({ nom: n, pts: h.map((x) => [x[0], (x[4] || [])[i]]) })))}</div>
      ${a.eau != null || (a.historique || []).some((x) => x[2] != null) ? `<div class="sec"><h3>Eau du circuit : ${fmt(a.eau)} °C</h3>${this._courbes(a, "e", "de l'eau", (h) => [{ nom: "Eau", pts: h.map((x) => [x[0], x[2]]) }])}</div>` : ""}
      <details open><summary>Ce que j'ai appris (${Math.round((ap.confiance || 0) * 100)} % de confiance)</summary>
        <ul><li>Délai d'allumage : ${ap.delai_appris ? `appris sur ${(ap.delais || []).length} essai(s), prudent ≈ ${Math.round(ap.delai_p80)} min` : "valeur par défaut 20 min, pas encore appris"}</li>${piecesAppris}</ul>
        <p class="petit">La confiance monte avec les allumages observés. Tant qu'une valeur est « par défaut », le calcul est volontairement prudent.</p></details>
      <details><summary>Garde-fous actifs</summary><ul>
        <li>Allumage automatique autorisé entre ${esc(gf.fenetre)}</li><li>Allumages automatiques aujourd'hui : ${gf.allumages_aujourdhui} / ${gf.max_allumages}</li>
        <li>Attente minimale entre deux allumages : ${gf.arret_min} min</li><li>Marche minimale avant arrêt automatique : ${gf.marche_min} min</li>
        <li>Arrêt de sécurité d'un allumage automatique : ${gf.marche_max} min</li><li>Jamais d'allumage dans les 20 minutes qui précèdent l'échéance</li></ul></details>
      <details><summary>Derniers cycles</summary><ul>${journal}</ul>${da ? `<p class="petit">Dernière commande : ${esc(da.service === "turn_on" ? "allumage" : "arrêt")} (${esc(da.source)}) à ${jjmm(da.ts)}${da.erreur ? " — refusée : " + esc(da.erreur) : ""}</p>` : ""}
        <p class="petit">État brut lu sur le poêle : « ${esc(a.statut_brut)} »</p></details>
    </ha-card>`;
    r.getElementById("allumer")?.addEventListener("click", () => this._appel("button", "press", b.allumer));
    r.getElementById("annuler")?.addEventListener("click", () => this._appel("button", "press", b.annuler));
    for (const cle of ["auto", "arret_auto", "absent"]) r.getElementById(cle)?.addEventListener("click", () => this._appel("switch", d[cle] ? "turn_off" : "turn_on", b[cle]));
  }
}

class PilotagePoeleCardEditor extends HTMLElement {
  setConfig(c) { this._config = c; this._rendre(); }
  set hass(h) { this._hass = h; this._rendre(); }
  _rendre() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = () => "Entité « État du poêle »";
      this._form.addEventListener("value-changed", (e) => this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: e.detail.value }, bubbles: true, composed: true })));
      this.appendChild(this._form);
    }
    this._form.hass = this._hass; this._form.data = this._config;
    this._form.schema = [{ name: "entity", required: true, selector: { entity: { domain: "sensor", integration: "pilotage_poele", device_class: "enum" } } }];
  }
}
customElements.get("pilotage-poele-card") || customElements.define("pilotage-poele-card", PilotagePoeleCard);
customElements.get("pilotage-poele-card-editor") || customElements.define("pilotage-poele-card-editor", PilotagePoeleCardEditor);
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "pilotage-poele-card")) window.customCards.push({ type: "pilotage-poele-card", name: "Pilotage poêle", description: "Prochain allumage et pourquoi, frise 24 h, pièces, apprentissage, garde-fous.", preview: false });
console.info(`%c PILOTAGE-POELE %c ${VERSION}`, "background:#1F5A4A;color:#fff", "");
