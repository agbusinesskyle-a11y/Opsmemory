// OpsMemory Triage prototype — fake data, full keyboard, sticky bulk bar.

const { useState, useEffect, useRef, useMemo, useCallback } = React;

/* ───────────────────────────── tweaks ─────────────────────────────────── */

const TRIAGE_TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "dark",
  "density": "compact",
  "accentHue": 232,
  "showShortcuts": true
}/*EDITMODE-END*/;

/* ───────────────────────────── data ───────────────────────────────────── */

// minutes ago helpers
const MIN = 1, H = 60, D = 24 * 60;

const SAMPLE = [
  { id: "ri_001", action: "CREATE",   conf: 0.91, ageMin: 3*D + 4*H,
    src: { kind:"slack", channel:"#ops-redhot",   author:"miguel.a", at:"May 6 · 09:14",
           quote:"yo we're way short on cones at the mesa stand, like 6 left. need to reorder before the jun 1 weekend." },
    summary:"Order replacement cones for Mesa stand", business:"redhot",
    flags:["redhot","due:jun 1"], dueAt:"Jun 1",
    candidates:[{ name:"Order traffic cones (RedHot stock 2025)", conf:0.34 }],
    validation:[], assignee:"kyle", priority:"normal" },

  { id: "ri_002", action: "UPDATE",   conf: 0.84, ageMin: 1*D + 6*H,
    src: { kind:"recap", channel:"recap May 8", author:"meeting", at:"May 8 · weekly ops",
           quote:"Open checklist needs the espresso warm-up step before lights — Maya kept hitting cold pulls last week." },
    summary:"Reorder opener checklist: espresso warm-up before lights", business:"redhot",
    flags:["sop:store-opening v3","conflict"], dueAt:null,
    candidates:[{ name:"SOP: Store opening v3 (RedHot)", conf:0.78 },
                { name:"SOP: Store opening v2 — superseded", conf:0.41 }],
    validation:["Conflicts with SOP step 3 anchor"], assignee:"maya", priority:"normal" },

  { id: "ri_003", action: "COMPLETE", conf: 0.96, ageMin: 2*H,
    src: { kind:"slack", channel:"#ops-borderline", author:"kyle", at:"May 9 · 11:42",
           quote:"submitted the SD permit packet just now — health, fire, ABC. confirmation #SD-228841." },
    summary:"Submit San Diego permit packet (health/fire/ABC)", business:"borderline",
    flags:["borderline"], dueAt:null,
    candidates:[{ name:"Submit SD permit packet for May tour", conf:0.94 }],
    validation:[], assignee:"kyle", priority:"high" },

  { id: "ri_004", action: "CREATE",   conf: 0.78, ageMin: 5*H,
    src: { kind:"slack", channel:"#ops-redhot", author:"jt", at:"May 9 · 09:01",
           quote:"generator at mesa is making that whine again. we should get the inspection booked, last one was Feb." },
    summary:"Schedule generator inspection at Mesa", business:"redhot",
    flags:["redhot"], dueAt:null,
    candidates:[], validation:[], assignee:null, priority:"normal" },

  { id: "ri_005", action: "AMBIGUOUS",conf: 0.52, ageMin: 4*D + 1*H,
    src: { kind:"recap", channel:"recap May 5", author:"meeting", at:"May 5 · syncs",
           quote:"…talk to Roy about the insurance, see if we can roll the new trailer onto the existing rider — or break it out, hard to say." },
    summary:"Talk to Roy re: insurance — roll new trailer onto rider OR break out?", business:null,
    flags:["ambiguous"], dueAt:null,
    candidates:[{ name:"Renew insurance — RedHot fleet 2026", conf:0.31 },
                { name:"New trailer — finance + register", conf:0.27 }],
    validation:["Two plausible parents — needs reviewer pick"], assignee:null, priority:"normal" },

  { id: "ri_006", action: "CREATE",   conf: 0.93, ageMin: 12*H,
    src: { kind:"slack", channel:"#ops-redhot", author:"jt", at:"May 8 · 21:30",
           quote:"the POS signs at mesa are toast. weather got em. need new ones before next weekend ideally." },
    summary:"Replace POS signage at Mesa stand", business:"redhot",
    flags:["redhot","due:May 20"], dueAt:"May 20",
    candidates:[], validation:[], assignee:"kyle", priority:"high" },

  { id: "ri_007", action: "UPDATE",   conf: 0.71, ageMin: 2*D + 3*H,
    src: { kind:"file", channel:"file drop · menu_v9.pdf", author:"sarah", at:"May 7 · upload",
           quote:"Updated card with allergen icons (gluten, dairy, sesame). Replace at all 3 stands by next Sat." },
    summary:"Add allergen icons to printed menu cards (GF/DF/sesame)", business:"borderline",
    flags:["borderline"], dueAt:"May 17",
    candidates:[{ name:"Reprint menu cards Q2", conf:0.66 }],
    validation:[], assignee:"sarah", priority:"normal" },

  { id: "ri_008", action: "COMPLETE", conf: 0.89, ageMin: 3*H,
    src: { kind:"slack", channel:"#ops-redhot", author:"kyle", at:"May 9 · 10:22",
           quote:"AZ vendor permit paid — $342, ref AZ-VP-2026-1187. that's done." },
    summary:"Pay AZ vendor permit — RedHot Mesa 2026", business:"redhot",
    flags:[], dueAt:null,
    candidates:[{ name:"Pay AZ vendor permit (Mesa Q2)", conf:0.91 }],
    validation:[], assignee:"kyle", priority:"normal" },

  { id: "ri_009", action: "CREATE",   conf: 0.45, ageMin: 1*D + 2*H,
    src: { kind:"file", channel:"file drop · receipt_napkins.png", author:"jt", at:"May 8 · upload",
           quote:"(image of napkin invoice — supplier, qty, price not OCR'd cleanly)" },
    summary:"Reorder branded napkins (supplier unclear)", business:null,
    flags:["low conf"], dueAt:null,
    candidates:[], validation:["OCR ambiguous — supplier missing","No business attribution"],
    assignee:null, priority:"normal" },

  { id: "ri_010", action: "AMBIGUOUS",conf: 0.58, ageMin: 6*H,
    src: { kind:"slack", channel:"#ops-borderline", author:"sarah", at:"May 9 · 06:50",
           quote:"thinking maybe we shift the truck route to hit Pacific Beach Sat morning instead of evening? or both? idk" },
    summary:"Maybe shift truck route to Pacific Beach Sat AM (vs PM)?", business:"borderline",
    flags:["borderline","ambiguous"], dueAt:null,
    candidates:[], validation:["Operator decision required — not a task yet"], assignee:null, priority:"normal" },

  { id: "ri_011", action: "CREATE",   conf: 0.95, ageMin: 30*MIN,
    src: { kind:"slack", channel:"#ops-borderline", author:"kyle", at:"May 9 · 13:15",
           quote:"@OpsMemory remind me — confirm the SD tent delivery for next Sat with Tony, latest by Wed." },
    summary:"Confirm SD tent delivery with Tony", business:"borderline",
    flags:["borderline","due:May 13"], dueAt:"May 13",
    candidates:[], validation:[], assignee:"kyle", priority:"high" },

  { id: "ri_012", action: "UPDATE",   conf: 0.88, ageMin: 1*H,
    src: { kind:"slack", channel:"#ops-redhot", author:"kyle", at:"May 9 · 12:45",
           quote:"swap weekend opener — Maya in for Devon Sat + Sun. Devon's got family stuff." },
    summary:"Reassign weekend opener: Maya for Devon (Sat+Sun)", business:"redhot",
    flags:["redhot"], dueAt:null,
    candidates:[{ name:"Weekend opener schedule — May", conf:0.92 }],
    validation:[], assignee:"maya", priority:"normal" },

  { id: "ri_013", action: "CREATE",   conf: 0.67, ageMin: 8*H,
    src: { kind:"file", channel:"file drop · propane_count.xlsx", author:"jt", at:"May 9 · upload",
           quote:"(spreadsheet — last inventory line dated 04/12, 14 tanks; need recount before tour)" },
    summary:"Re-inventory propane tanks before tour", business:"redhot",
    flags:[], dueAt:null,
    candidates:[], validation:[], assignee:null, priority:"normal" },

  { id: "ri_014", action: "COMPLETE", conf: 0.92, ageMin: 4*H,
    src: { kind:"slack", channel:"#ops-borderline", author:"sarah", at:"May 9 · 09:30",
           quote:"health dept renewal — paid, filed, certificate emailed. SD county done for the year." },
    summary:"Submit San Diego health dept annual renewal", business:"borderline",
    flags:["borderline"], dueAt:null,
    candidates:[{ name:"SD County health renewal 2026", conf:0.96 }],
    validation:[], assignee:"sarah", priority:"normal" },

  { id: "ri_015", action: "CREATE",   conf: 0.84, ageMin: 5*MIN,
    src: { kind:"slack", channel:"#ops-redhot", author:"jt", at:"May 9 · 13:40",
           quote:"!! ice machine at mesa is dead. like fully dead. we open in 16 hours." },
    summary:"Emergency: book ice machine repair at Mesa (open in <24h)", business:"redhot",
    flags:["redhot","urgent","due:today"], dueAt:"Today",
    candidates:[], validation:[], assignee:"kyle", priority:"urgent" },

  { id: "ri_016", action: "AMBIGUOUS",conf: 0.49, ageMin: 4*D + 8*H,
    src: { kind:"recap", channel:"recap May 5", author:"meeting", at:"May 5 · weekly",
           quote:"…possibly hire a 2nd weekend cook? Mesa is getting slammed but Borderline is fine. circle back." },
    summary:"Possibly hire 2nd weekend cook (Mesa-only)", business:null,
    flags:["ambiguous","stale"], dueAt:null,
    candidates:[], validation:["No firm decision — circle-back item","No assignee"],
    assignee:null, priority:"normal" },

  { id: "ri_017", action: "UPDATE",   conf: 0.87, ageMin: 18*H,
    src: { kind:"slack", channel:"#ops-redhot", author:"kyle", at:"May 8 · 19:10",
           quote:"new sat close time at mesa is 10pm not 11. update the schedule + door sign + GMB hours." },
    summary:"Adjust Saturday close time to 10pm at Mesa (3 surfaces)", business:"redhot",
    flags:["redhot"], dueAt:null,
    candidates:[{ name:"Mesa hours — schedule v4", conf:0.81 }],
    validation:[], assignee:"jt", priority:"normal" },

  { id: "ri_018", action: "CREATE",   conf: 0.79, ageMin: 2*D + 1*H,
    src: { kind:"slack", channel:"#ops-borderline", author:"sarah", at:"May 7 · 12:08",
           quote:"uniform shirts are looking ratty. let's get a new run — same supplier as last time, +10 to inventory." },
    summary:"Order new uniform shirts (Borderline run, +10 inventory)", business:"borderline",
    flags:["borderline"], dueAt:null,
    candidates:[{ name:"Uniform reorder — Borderline 2025", conf:0.61 }],
    validation:[], assignee:"sarah", priority:"normal" },

  { id: "ri_019", action: "CREATE",   conf: 0.91, ageMin: 1*D + 3*H,
    src: { kind:"recap", channel:"recap May 8", author:"meeting", at:"May 8 · weekly ops",
           quote:"schedule Q3 vendor reviews — propane, dry goods, paper. owners to lead, kyle has the template." },
    summary:"Schedule Q3 vendor reviews (propane / dry goods / paper)", business:"redhot",
    flags:["redhot"], dueAt:"Jul 1",
    candidates:[], validation:[], assignee:"kyle", priority:"normal" },

  { id: "ri_020", action: "COMPLETE", conf: 0.94, ageMin: 1*H,
    src: { kind:"slack", channel:"#ops-redhot", author:"jt", at:"May 9 · 12:50",
           quote:"first-aid kit restocked — gauze, burn gel, eyewash, ibuprofen. all 4 stands." },
    summary:"Restock first-aid kits (4 stands)", business:"redhot",
    flags:[], dueAt:null,
    candidates:[{ name:"First-aid restock — May", conf:0.93 }],
    validation:[], assignee:"jt", priority:"normal" },

  { id: "ri_021", action: "UPDATE",   conf: 0.62, ageMin: 9*H,
    src: { kind:"slack", channel:"#ops-borderline", author:"sarah", at:"May 9 · 04:55",
           quote:"city changed our trash pickup to tuesdays starting next week. update the closer checklist." },
    summary:"Move trash pickup day to Tuesday in closer SOP", business:"borderline",
    flags:["borderline","sop:closer v2","conflict"], dueAt:null,
    candidates:[{ name:"SOP: Closer v2 (Borderline)", conf:0.74 },
                { name:"SOP: Closer v1 — superseded", conf:0.31 }],
    validation:["Step 8 conflicts with current Mon pickup"], assignee:"sarah", priority:"normal" },
];

/* completed-today fixture (separate stream so the count is real) */
const COMPLETED_TODAY = [
  { id:"ct_1", action:"CREATE",   summary:"Order propane refills (4 tanks, RedHot)",     when:"08:14", by:"kyle", conf:0.94 },
  { id:"ct_2", action:"COMPLETE", summary:"Pay AZ vendor permit",                         when:"10:22", by:"kyle", conf:0.89 },
  { id:"ct_3", action:"UPDATE",   summary:"Reassign Tuesday opener: JT for Devon",        when:"11:01", by:"kyle", conf:0.86 },
  { id:"ct_4", action:"CREATE",   summary:"Add Saturday Pacific Beach trial slot",        when:"11:45", by:"kyle", conf:0.81 },
  { id:"ct_5", action:"COMPLETE", summary:"Submit health dept renewal (Borderline)",      when:"12:08", by:"kyle", conf:0.92 },
];

/* ───────────────────────────── helpers ────────────────────────────────── */

function ageStr(min) {
  if (min < 60) return `${min}m`;
  if (min < 24*60) return `${Math.floor(min/60)}h`;
  return `${Math.floor(min/(24*60))}d`;
}
function ageBucket(min) {
  if (min < 24*60) return "fresh";
  if (min < 3*24*60) return "warm";
  if (min < 7*24*60) return "stale";
  return "vstale";
}
function actionClass(a) {
  return a === "CREATE" ? "create"
       : a === "UPDATE" ? "update"
       : a === "COMPLETE" ? "complete"
       : "amb";
}
function confTier(c) {
  if (c >= 0.85) return "high";
  if (c >= 0.70) return "med";
  if (c >= 0.55) return "low";
  return "vlow";
}

/* ───────────────────────────── icons ──────────────────────────────────── */

const Icon = {
  Slack: (p) => (
    <svg className={p.className} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M5.5 14.5a1.75 1.75 0 1 0 0 3.5 1.75 1.75 0 0 0 0-3.5Zm0-7.75a1.75 1.75 0 1 1 0-3.5 1.75 1.75 0 0 1 0 3.5Zm12.5 0a1.75 1.75 0 1 1 0-3.5 1.75 1.75 0 0 1 0 3.5Zm0 13.5a1.75 1.75 0 1 1 0-3.5 1.75 1.75 0 0 1 0 3.5ZM10 6.75v4.5h4.5v-4.5h-4.5Zm0 6h4.5v4.5h-4.5v-4.5Z"/>
    </svg>
  ),
  Doc: (p) => (
    <svg className={p.className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z"/>
      <path d="M14 3v5h5M9 13h6M9 17h4"/>
    </svg>
  ),
  File: (p) => (
    <svg className={p.className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M5 4h9l5 5v11H5z"/><path d="M14 4v5h5"/>
    </svg>
  ),
  Inbox: (p)=> (<svg className={p.className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"><path d="M4 13h5l1.5 2h3l1.5-2h5"/><path d="M5 13l2-7h10l2 7v6H5z"/></svg>),
  Stale: (p)=> (<svg className={p.className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/></svg>),
  Snooze: (p)=> (<svg className={p.className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"><path d="M9 7h6l-6 8h6"/><circle cx="12" cy="12" r="9"/></svg>),
  Check: (p)=> (<svg className={p.className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"><path d="M5 12l5 5 9-11"/></svg>),
  Warn:  (p)=> (<svg className={p.className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 3l10 17H2z"/><path d="M12 9v5M12 17v.5"/></svg>),
  Search:(p)=> (<svg className={p.className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="11" cy="11" r="6"/><path d="M20 20l-4-4"/></svg>),
  X:     (p)=> (<svg className={p.className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M5 5l14 14M19 5L5 19"/></svg>),
};

const SourceIcon = ({kind}) => {
  const cls = "source-icon";
  if (kind === "slack") return <Icon.Slack className={cls} />;
  if (kind === "recap") return <Icon.Doc className={cls} />;
  return <Icon.File className={cls} />;
};

/* sparkline */
const Spark = ({values, w=80, h=18}) => {
  const max = Math.max(...values, 1);
  const step = w / (values.length - 1);
  const pts = values.map((v,i)=>`${(i*step).toFixed(1)},${(h - (v/max)*h).toFixed(1)}`).join(" ");
  return (
    <svg className="spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`}>
      <polyline fill="none" stroke="currentColor" strokeWidth="1.4" points={pts} />
    </svg>
  );
};

/* ───────────────────────────── app ────────────────────────────────────── */

function App() {
  const [t, setTweak] = useTweaks(TRIAGE_TWEAK_DEFAULTS);

  // apply theme + density to <html>
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", t.theme);
    document.documentElement.setAttribute("data-density", t.density);
    document.documentElement.style.setProperty("--acc-h", String(t.accentHue));
  }, [t.theme, t.density, t.accentHue]);

  const [subview, setSubview] = useState("inbox"); // inbox | stale | snoozed | completed
  const [items, setItems] = useState(() => SAMPLE.map(s => ({...s, status:"pending", snoozedUntil:null, rejectedReason:null})));
  const [completedToday] = useState(COMPLETED_TODAY);
  const [focusIdx, setFocusIdx] = useState(0);
  const [selected, setSelected] = useState(new Set()); // ids
  const [anchorIdx, setAnchorIdx] = useState(0);
  const [detailOpen, setDetailOpen] = useState(true);
  const [modal, setModal] = useState(null); // {kind, ids|id}
  const [editingId, setEditingId] = useState(null);
  const [showHelp, setShowHelp] = useState(false);
  const [toast, setToast] = useState(null);
  const [forecastFilter, setForecastFilter] = useState(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState("");

  const listRef = useRef(null);

  // visible items per subview
  const visible = useMemo(() => {
    let xs = items;
    if (subview === "inbox")
      xs = xs.filter(x => x.status === "pending");
    else if (subview === "stale")
      xs = xs.filter(x => x.status === "pending" && x.ageMin >= 3*24*60);
    else if (subview === "snoozed")
      xs = xs.filter(x => x.status === "snoozed");
    else if (subview === "completed")
      xs = []; // rendered separately
    if (forecastFilter === "fresh")  xs = xs.filter(x => x.ageMin < 24*60);
    if (forecastFilter === "warm")   xs = xs.filter(x => x.ageMin >= 24*60 && x.ageMin < 3*24*60);
    if (forecastFilter === "stale")  xs = xs.filter(x => x.ageMin >= 3*24*60);
    if (forecastFilter === "urgent") xs = xs.filter(x => x.priority === "urgent" || x.flags.includes("urgent") || (x.dueAt === "Today"));
    if (search.trim())
      xs = xs.filter(x => (x.summary + " " + x.src.channel + " " + x.src.quote).toLowerCase().includes(search.toLowerCase()));
    return xs;
  }, [items, subview, forecastFilter, search]);

  // counts
  const counts = useMemo(() => {
    const pend = items.filter(x => x.status === "pending");
    return {
      inbox: pend.length,
      stale: pend.filter(x => x.ageMin >= 3*24*60).length,
      snoozed: items.filter(x => x.status === "snoozed").length,
      completed: completedToday.length + items.filter(x=>x.status==="approved").length,
      fresh: pend.filter(x => x.ageMin < 24*60).length,
      warm: pend.filter(x => x.ageMin >= 24*60 && x.ageMin < 3*24*60).length,
      vstale: pend.filter(x => x.ageMin >= 7*24*60).length,
      urgent: pend.filter(x => x.priority === "urgent" || x.flags.includes("urgent") || x.dueAt === "Today").length,
      avgConf: (pend.reduce((s,x)=>s+x.conf,0) / Math.max(pend.length,1)),
    };
  }, [items, completedToday]);

  // keep focusIdx in range
  useEffect(() => {
    if (focusIdx >= visible.length) setFocusIdx(Math.max(0, visible.length - 1));
  }, [visible.length, focusIdx]);

  // scroll focused into view
  useEffect(() => {
    if (!listRef.current) return;
    const el = listRef.current.querySelector(`[data-row-idx="${focusIdx}"]`);
    if (!el) return;
    const r = el.getBoundingClientRect();
    const pr = listRef.current.getBoundingClientRect();
    if (r.top < pr.top + 30) listRef.current.scrollTop -= (pr.top + 30 - r.top);
    else if (r.bottom > pr.bottom - 8) listRef.current.scrollTop += (r.bottom - pr.bottom + 8);
  }, [focusIdx]);

  /* selection helpers */
  const isSelected = (id) => selected.has(id);
  const toggleSelect = (idx, withShift = false) => {
    const id = visible[idx]?.id; if (!id) return;
    setSelected(prev => {
      const next = new Set(prev);
      if (withShift) {
        const lo = Math.min(anchorIdx, idx), hi = Math.max(anchorIdx, idx);
        for (let i = lo; i <= hi; i++) next.add(visible[i].id);
      } else {
        if (next.has(id)) next.delete(id); else next.add(id);
        setAnchorIdx(idx);
      }
      return next;
    });
  };
  const selectAll = () => setSelected(new Set(visible.map(v => v.id)));
  const clearSel = () => setSelected(new Set());

  /* actions */
  const flash = (msg) => { setToast(msg); setTimeout(()=>setToast(null), 1800); };

  const applyApprove = (ids) => {
    setItems(prev => prev.map(x => ids.includes(x.id) ? {...x, status:"approved"} : x));
    flash(`Approved ${ids.length}`);
    setSelected(new Set());
  };
  const applyReject = (ids, reason) => {
    setItems(prev => prev.map(x => ids.includes(x.id) ? {...x, status:"rejected", rejectedReason:reason} : x));
    flash(`Rejected ${ids.length}${reason?` — "${reason}"`:""}`);
    setSelected(new Set());
  };
  const applySnooze = (ids, untilLabel) => {
    setItems(prev => prev.map(x => ids.includes(x.id) ? {...x, status:"snoozed", snoozedUntil:untilLabel} : x));
    flash(`Snoozed ${ids.length} until ${untilLabel}`);
    setSelected(new Set());
  };

  const focusedItem = visible[focusIdx];
  const targetIds = () => selected.size > 0 ? [...selected] : (focusedItem ? [focusedItem.id] : []);

  /* keyboard */
  useEffect(() => {
    let gPressed = false, gTimer = null;
    const onKey = (e) => {
      if (modal) {
        if (e.key === "Escape") { setModal(null); }
        return;
      }
      // editing inline → don't capture global keys
      if (editingId) {
        if (e.key === "Escape") { setEditingId(null); }
        return;
      }
      const target = e.target;
      const inField = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      if (inField && !["Escape"].includes(e.key)) return;

      if (e.key === "?" && !e.metaKey && !e.ctrlKey) { e.preventDefault(); setShowHelp(s=>!s); return; }
      if (e.key === "Escape") {
        if (searchOpen) { setSearchOpen(false); setSearch(""); return; }
        if (selected.size) { clearSel(); return; }
        if (forecastFilter) { setForecastFilter(null); return; }
        return;
      }

      // G then R/T/S
      if (gPressed) {
        if (e.key === "r") { setSubview("inbox"); flash("→ Triage"); }
        if (e.key === "t") { flash("→ Tasks (mock)"); }
        if (e.key === "s") { flash("→ SOPs (mock)"); }
        gPressed = false; clearTimeout(gTimer); return;
      }
      if (e.key === "g" && !e.metaKey && !e.ctrlKey) {
        gPressed = true; gTimer = setTimeout(()=>{gPressed=false;}, 800); return;
      }

      if ((e.key === "k" || e.key === "K" || e.key === "ArrowUp") && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        if (e.shiftKey) {
          const next = Math.max(0, focusIdx - 1);
          setSelected(prev => { const n = new Set(prev); n.add(visible[next]?.id); n.add(visible[focusIdx]?.id); return n; });
          setFocusIdx(next);
        } else setFocusIdx(i => Math.max(0, i - 1));
        return;
      }
      if ((e.key === "j" || e.key === "J" || e.key === "ArrowDown") && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        if (e.shiftKey) {
          const next = Math.min(visible.length - 1, focusIdx + 1);
          setSelected(prev => { const n = new Set(prev); n.add(visible[next]?.id); n.add(visible[focusIdx]?.id); return n; });
          setFocusIdx(next);
        } else setFocusIdx(i => Math.min(visible.length - 1, i + 1));
        return;
      }
      if (e.key === "x" || e.key === "X") { e.preventDefault(); toggleSelect(focusIdx, e.shiftKey); return; }
      if ((e.key === "a" || e.key === "A") && (e.metaKey || e.ctrlKey)) { e.preventDefault(); selectAll(); return; }
      if (e.key === "Enter") { e.preventDefault(); setDetailOpen(true); return; }

      if (e.key === "1") { e.preventDefault(); applyApprove(targetIds()); return; }
      if (e.key === "2") { e.preventDefault(); setModal({kind:"reject", ids:targetIds()}); return; }
      if (e.key === "3") {
        e.preventDefault();
        const ids = targetIds();
        if (ids.length === 1) { setEditingId(ids[0]); setDetailOpen(true); }
        else flash("Edit-then-approve: focus a single item");
        return;
      }
      if (e.key === "h" || e.key === "H") { e.preventDefault(); setModal({kind:"snooze", ids:targetIds()}); return; }

      if ((e.key === "k" && (e.metaKey || e.ctrlKey)) || e.key === "/") {
        e.preventDefault(); setSearchOpen(true);
        setTimeout(()=>document.querySelector(".search input")?.focus(), 10);
        return;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focusIdx, visible, selected, modal, editingId, searchOpen, forecastFilter]);

  /* ──── render parts ──── */

  const fc = (label, num, key, tone) => (
    <div className={"fc" + (forecastFilter===key?" active":"")} onClick={()=>setForecastFilter(forecastFilter===key?null:key)}>
      <div className="fc-lbl">{label}</div>
      <div className={"fc-num" + (tone?" "+tone:"")}>{num}</div>
      <Spark values={key==="urgent"?[1,1,2,2,3,2,4]:key==="stale"?[2,3,3,4,4,5,4]:key==="warm"?[6,7,5,8,7,9,8]:[10,11,9,12,11,13,12]} />
    </div>
  );

  return (
    <div className="app">
      {/* Left rail */}
      <aside className="rail">
        <div className="rail-brand">
          <span className="dot"></span>
          <span>OpsMemory</span>
        </div>
        <div className="rail-sec">Workspace</div>
        <div className="rail-item">
          <Icon.Inbox className="rail-icon" />
          <span>Tasks</span>
        </div>
        <div className="rail-item active">
          <Icon.Stale className="rail-icon" />
          <span>Triage</span>
          <span className="badge">{counts.inbox}</span>
        </div>
        <div className="rail-item">
          <Icon.Doc className="rail-icon" />
          <span>SOPs</span>
        </div>
        <div className="rail-item">
          <Icon.File className="rail-icon" />
          <span>Activity</span>
        </div>
        <div className="rail-sec">Spaces</div>
        <div className="rail-item">
          <span className="rail-icon" style={{background:"var(--c-redhot)",borderRadius:3,height:8,width:8,marginLeft:3}}></span>
          <span>RedHot</span>
          <span className="badge">{items.filter(i=>i.business==="redhot"&&i.status==="pending").length}</span>
        </div>
        <div className="rail-item">
          <span className="rail-icon" style={{background:"var(--c-border)",borderRadius:3,height:8,width:8,marginLeft:3}}></span>
          <span>Borderline</span>
          <span className="badge">{items.filter(i=>i.business==="borderline"&&i.status==="pending").length}</span>
        </div>
        <div className="rail-foot">
          <span className="avatar">K</span>
          <span style={{flex:1}}>kyle</span>
          <span className="pill-kbd" title="Shortcuts" onClick={()=>setShowHelp(s=>!s)}>?</span>
        </div>
      </aside>

      {/* Main */}
      <main className="main">
        {/* topbar */}
        <div className="topbar">
          <div className="breadcrumb">
            Workspace<span style={{color:"var(--fg-faint)"}}>/</span><b>Triage</b>
          </div>
          <div className="grow"></div>
          <span className="pill-kbd" style={{cursor:"default"}} onClick={()=>setSearchOpen(true)}>⌘K</span>
          <span className="pill-kbd" onClick={()=>setShowHelp(s=>!s)}>?</span>
          <button className="iconbtn" title="Toggle detail pane" onClick={()=>setDetailOpen(o=>!o)}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M4 5h16v14H4z"/><path d="M14 5v14"/>
            </svg>
          </button>
        </div>

        {/* head */}
        <div className="head">
          <div className="head-title">
            <div className="h1">Triage</div>
            <div className="head-sub">{counts.inbox} pending · avg confidence {(counts.avgConf*100).toFixed(0)}%</div>
          </div>
          <div className="subtabs">
            {[
              ["inbox","Inbox", counts.inbox],
              ["stale","Stale", counts.stale],
              ["snoozed","Snoozed", counts.snoozed],
              ["completed","Completed today", counts.completed],
            ].map(([k,l,c]) => (
              <button key={k} className={"subtab" + (subview===k?" active":"")} onClick={()=>{setSubview(k); setForecastFilter(null);}}>
                <span>{l}</span><span className="ct">{c}</span>
              </button>
            ))}
            <div className="grow"></div>
            {searchOpen ? (
              <div className="search" style={{margin:"0 0 6px"}}>
                <Icon.Search className="source-icon" />
                <input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Filter rows…" />
                <button className="iconbtn" style={{width:20,height:20,border:0}} onClick={()=>{setSearchOpen(false);setSearch("");}}>
                  <Icon.X className="source-icon" />
                </button>
              </div>
            ) : (
              <button className="subtab" onClick={()=>setSearchOpen(true)}>
                <Icon.Search className="source-icon" />
                <span style={{fontSize:11.5,color:"var(--fg-mut)"}}>Filter</span>
                <span className="pill-kbd" style={{marginLeft:4}}>/</span>
              </button>
            )}
          </div>
        </div>

        {/* forecast strip */}
        <div className="forecast">
          {fc("Fresh today", counts.fresh, "fresh")}
          {fc("Pending 1–3d", counts.warm, "warm")}
          {fc("Stale 3d+", counts.stale, "stale", "warn")}
          {fc("Stale 7d+", counts.vstale, "vstale", "danger")}
          {fc("Due today / urgent", counts.urgent, "urgent", counts.urgent>0?"danger":"")}
        </div>

        {/* list + detail */}
        <div className={"listwrap" + (detailOpen?"":" no-detail")}>
          <div className="list" ref={listRef} tabIndex={0}>
            <div className="list-head">
              <div>Age</div>
              <div>Action</div>
              <div>Conf.</div>
              <div>Source</div>
              <div>Candidate</div>
              <div style={{textAlign:"right"}}>Flags</div>
            </div>
            {subview === "completed" ? (
              <CompletedList items={completedToday} approved={items.filter(x=>x.status==="approved")} />
            ) : visible.length === 0 ? (
              <EmptyList subview={subview} />
            ) : (
              visible.map((x, i) => (
                <Row
                  key={x.id} item={x} idx={i}
                  focused={i===focusIdx} selected={isSelected(x.id)}
                  onClick={(e)=>{ setFocusIdx(i); if (e.metaKey || e.ctrlKey || e.shiftKey) toggleSelect(i, e.shiftKey); }}
                  onCheck={(e)=>{ e.stopPropagation(); toggleSelect(i, e.shiftKey); }}
                />
              ))
            )}
          </div>
          {detailOpen && (
            <Detail
              item={focusedItem}
              editing={editingId === focusedItem?.id}
              onCloseEdit={()=>setEditingId(null)}
              onApprove={()=>focusedItem && applyApprove([focusedItem.id])}
              onReject={()=>focusedItem && setModal({kind:"reject", ids:[focusedItem.id]})}
              onSnooze={()=>focusedItem && setModal({kind:"snooze", ids:[focusedItem.id]})}
              onEdit={()=>focusedItem && setEditingId(focusedItem.id)}
              onSaveEdit={(patch)=>{
                setItems(prev => prev.map(x=>x.id===editingId?{...x,...patch,status:"approved"}:x));
                setEditingId(null);
                flash(`Edited & approved`);
              }}
            />
          )}
        </div>
      </main>

      {/* Tweaks panel */}
      <TweaksPanel>
        <TweakSection label="Theme" />
        <TweakRadio label="Mode" value={t.theme} options={["dark","light"]} onChange={v=>setTweak("theme", v)} />
        <TweakSlider label="Accent hue" value={t.accentHue} min={0} max={360} step={5} unit="°"
                     onChange={v=>setTweak("accentHue", v)} />
        <TweakSection label="Density" />
        <TweakRadio label="Rows" value={t.density} options={["compact","cozy","comfy"]} onChange={v=>setTweak("density", v)} />
        <TweakSection label="Help" />
        <TweakToggle label="Show shortcut overlay" value={t.showShortcuts && showHelp}
                     onChange={v=>{setShowHelp(v); setTweak("showShortcuts", v);}} />
      </TweaksPanel>

      {/* sticky bulk bar */}
      <BulkBar
        count={selected.size}
        onApprove={()=>applyApprove([...selected])}
        onReject={()=>setModal({kind:"reject", ids:[...selected]})}
        onSnooze={()=>setModal({kind:"snooze", ids:[...selected]})}
        onClear={clearSel}
        items={items.filter(x=>selected.has(x.id))}
      />

      {/* shortcut overlay */}
      {showHelp && <Help onClose={()=>setShowHelp(false)} />}

      {/* toast */}
      {toast && (
        <div className="toast">
          <Icon.Check className="ic ok" />
          <span>{toast}</span>
        </div>
      )}

      {/* modals */}
      {modal?.kind === "reject" && (
        <RejectModal
          ids={modal.ids}
          items={items.filter(x=>modal.ids.includes(x.id))}
          onCancel={()=>setModal(null)}
          onSubmit={(reason)=>{ applyReject(modal.ids, reason); setModal(null); }}
        />
      )}
      {modal?.kind === "snooze" && (
        <SnoozeModal
          ids={modal.ids}
          items={items.filter(x=>modal.ids.includes(x.id))}
          onCancel={()=>setModal(null)}
          onSubmit={(label)=>{ applySnooze(modal.ids, label); setModal(null); }}
        />
      )}
    </div>
  );
}

/* ───────────────────────────── row ────────────────────────────────────── */

function Row({item, idx, focused, selected, onClick, onCheck}) {
  const status = item.status;
  const cls = [
    "row",
    focused && "focused",
    selected && "selected",
    status === "approved" && "approved",
    status === "snoozed" && "suppressed",
  ].filter(Boolean).join(" ");

  const ageBkt = ageBucket(item.ageMin);
  const ac = actionClass(item.action);
  const ct = confTier(item.conf);

  return (
    <div className={cls} data-row-idx={idx} onClick={onClick}>
      <div className="age">
        <span className="checkmark" onClick={onCheck}>
          {selected ? <Icon.Check style={{width:10,height:10}} /> : null}
        </span>
        {ageBkt === "stale" && <span className="stale-dot" title="3d+"></span>}
        {ageBkt === "vstale" && <span className="v-stale-dot" title="7d+"></span>}
        {ageStr(item.ageMin)}
      </div>
      <div className={"action " + ac}>
        {item.action === "AMBIGUOUS" ? "Ambig." : item.action.replace("_TASK","")}
      </div>
      <div className={"conf " + ct}>
        <span>{item.conf.toFixed(2)}</span>
        <div className="confbar"><i style={{width:`${item.conf*100}%`}}></i></div>
      </div>
      <div className="source">
        <SourceIcon kind={item.src.kind} />
        <span style={{overflow:"hidden",textOverflow:"ellipsis"}}>{item.src.channel}</span>
      </div>
      <div className="summary">
        {status === "approved" && <Icon.Check style={{width:11,height:11,marginRight:6,color:"var(--c-create)"}} />}
        {item.summary}
      </div>
      <div className="flags">
        {status === "approved" && <span className="chip approved">approved</span>}
        {status === "snoozed" && <span className="chip snoozed">snoozed · {item.snoozedUntil}</span>}
        {item.flags.includes("redhot") && <span className="chip redhot">RedHot</span>}
        {item.flags.includes("borderline") && <span className="chip borderline">Borderline</span>}
        {item.dueAt && <span className={"chip due" + (item.dueAt==="Today"?" over":"")}>due {item.dueAt}</span>}
        {item.flags.find(f=>f.startsWith("sop:")) && <span className="chip sop">SOP?</span>}
        {item.flags.includes("conflict") && <span className="chip conflict">conflict</span>}
        {item.flags.includes("ambiguous") && status==="pending" && <span className="chip conflict">ambiguous</span>}
        {item.flags.includes("urgent") && <span className="chip redhot">urgent</span>}
      </div>
    </div>
  );
}

function EmptyList({subview}) {
  const msg = {
    inbox: "Inbox is clear. Nice.",
    stale: "Nothing stale (3d+).",
    snoozed: "No snoozed candidates.",
    completed: "Nothing completed today yet.",
  }[subview];
  return <div style={{padding:"42px 16px",color:"var(--fg-dim)",fontSize:13,textAlign:"center"}}>{msg}</div>;
}

function CompletedList({items, approved}) {
  const all = [
    ...approved.map(a => ({id:a.id, action:a.action, summary:a.summary, when:"just now", by:"kyle", conf:a.conf})),
    ...items,
  ];
  return (
    <div>
      {all.map(x => (
        <div key={x.id} className="row" style={{opacity:.85}}>
          <div className="age" style={{color:"var(--fg-dim)"}}>{x.when}</div>
          <div className={"action " + actionClass(x.action)}>{x.action.replace("_TASK","")}</div>
          <div className="conf"><span>{x.conf.toFixed(2)}</span><div className="confbar"><i style={{width:`${x.conf*100}%`}}></i></div></div>
          <div className="source"><span style={{color:"var(--fg-mut)"}}>by {x.by}</span></div>
          <div className="summary" style={{textDecoration:"line-through",textDecorationColor:"var(--fg-faint)"}}>{x.summary}</div>
          <div className="flags"><span className="chip approved">applied</span></div>
        </div>
      ))}
    </div>
  );
}

/* ───────────────────────────── detail pane ─────────────────────────────── */

function Detail({item, editing, onCloseEdit, onApprove, onReject, onSnooze, onEdit, onSaveEdit}) {
  if (!item) return <div className="detail"><div className="detail-empty">No item focused.<br/>Use <span className="pill-kbd">J</span>/<span className="pill-kbd">K</span> to navigate.</div></div>;

  if (editing) return <DetailEdit item={item} onCancel={onCloseEdit} onSave={onSaveEdit} />;

  const ac = actionClass(item.action);
  const status = item.status;

  return (
    <div className="detail">
      <div className="detail-h">
        <div className="detail-meta">
          <span className={"action " + ac}>{item.action.replace("_TASK","")}</span>
          <span className="dot-sep">·</span>
          <span>conf {item.conf.toFixed(2)}</span>
          <span className="dot-sep">·</span>
          <span>pending {ageStr(item.ageMin)}</span>
          {status === "approved" && <span className="chip approved" style={{marginLeft:4}}>approved</span>}
          {status === "rejected" && <span className="chip" style={{background:"var(--c-redhot-bg)",color:"var(--c-redhot)"}}>rejected: {item.rejectedReason||"—"}</span>}
          {status === "snoozed" && <span className="chip snoozed" style={{marginLeft:4}}>snoozed · {item.snoozedUntil}</span>}
        </div>
        <h2 className="detail-title">{item.summary}</h2>
      </div>

      <div className="detail-body">
        <div className="sec">
          <div className="sec-h">Source</div>
          <div className="src-card">
            <div className="src-head">
              <SourceIcon kind={item.src.kind} />
              <span className="who">{item.src.author}</span>
              <span>·</span>
              <span>{item.src.channel}</span>
              <span>·</span>
              <span>{item.src.at}</span>
              <span style={{marginLeft:"auto",color:"var(--acc)",cursor:"default"}}>open raw event →</span>
            </div>
            <div className="src-quote">"{item.src.quote}"</div>
          </div>
        </div>

        <div className="sec">
          <div className="sec-h">Proposed facts</div>
          <dl className="kvgrid">
            <dt>summary</dt><dd>{item.summary}</dd>
            <dt>business</dt><dd>{item.business || <span style={{color:"var(--fg-dim)"}}>— missing —</span>}</dd>
            <dt>assignee</dt><dd>{item.assignee || <span style={{color:"var(--fg-dim)"}}>— unassigned —</span>}</dd>
            <dt>priority</dt><dd>{item.priority}</dd>
            <dt>due_at</dt><dd>{item.dueAt || <span style={{color:"var(--fg-dim)"}}>— none —</span>}</dd>
          </dl>
        </div>

        <div className="sec">
          <div className="sec-h">Retrieved candidates ({item.candidates.length})</div>
          {item.candidates.length === 0 ? (
            <div style={{color:"var(--fg-dim)",fontSize:12.5}}>No prior tasks matched — will create new.</div>
          ) : item.candidates.map((c,i)=>(
            <div key={i} className="candrow">
              <span>{c.name}</span>
              <span className="cand-conf">match {c.conf.toFixed(2)}</span>
            </div>
          ))}
        </div>

        <div className="sec">
          <div className="sec-h">Validation</div>
          {item.validation.length === 0 ? (
            <div style={{color:"var(--c-create)",fontSize:12.5,display:"flex",gap:6,alignItems:"center"}}>
              <Icon.Check style={{width:13,height:13}} /> No issues.
            </div>
          ) : item.validation.map((v,i)=>(
            <div key={i} style={{color:"var(--c-amb)",fontSize:12.5,display:"flex",gap:6,alignItems:"center",padding:"3px 0"}}>
              <Icon.Warn style={{width:13,height:13}} /> {v}
            </div>
          ))}
        </div>
      </div>

      {status === "pending" && (
        <div className="actions-row">
          <button className="btn primary" onClick={onApprove}>
            <span className="pill-kbd">1</span> Approve
          </button>
          <button className="btn" onClick={onEdit}>
            <span className="pill-kbd">3</span> Edit + approve
          </button>
          <button className="btn danger" onClick={onReject}>
            <span className="pill-kbd">2</span> Reject
          </button>
          <button className="btn" onClick={onSnooze}>
            <span className="pill-kbd">H</span> Snooze
          </button>
        </div>
      )}
    </div>
  );
}

function DetailEdit({item, onCancel, onSave}) {
  const [summary, setSummary] = useState(item.summary);
  const [business, setBusiness] = useState(item.business || "");
  const [assignee, setAssignee] = useState(item.assignee || "");
  const [priority, setPriority] = useState(item.priority);
  const [dueAt, setDueAt] = useState(item.dueAt || "");
  return (
    <div className="detail">
      <div className="detail-h">
        <div className="detail-meta">
          <span className={"action " + actionClass(item.action)}>{item.action.replace("_TASK","")}</span>
          <span className="dot-sep">·</span>
          <span>edit + approve</span>
        </div>
      </div>
      <div className="detail-body edit-form">
        <div>
          <div className="field-lbl">Summary</div>
          <input type="text" value={summary} onChange={e=>setSummary(e.target.value)} autoFocus />
        </div>
        <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:10}}>
          <div>
            <div className="field-lbl">Business</div>
            <select value={business} onChange={e=>setBusiness(e.target.value)}>
              <option value="">— none —</option>
              <option value="redhot">RedHot</option>
              <option value="borderline">Borderline</option>
            </select>
          </div>
          <div>
            <div className="field-lbl">Assignee</div>
            <select value={assignee} onChange={e=>setAssignee(e.target.value)}>
              <option value="">— unassigned —</option>
              <option value="kyle">kyle</option>
              <option value="jt">jt</option>
              <option value="maya">maya</option>
              <option value="sarah">sarah</option>
            </select>
          </div>
          <div>
            <div className="field-lbl">Priority</div>
            <select value={priority} onChange={e=>setPriority(e.target.value)}>
              <option value="urgent">urgent</option>
              <option value="high">high</option>
              <option value="normal">normal</option>
              <option value="low">low</option>
            </select>
          </div>
          <div>
            <div className="field-lbl">Due</div>
            <input type="text" value={dueAt} onChange={e=>setDueAt(e.target.value)} placeholder="e.g. May 20" />
          </div>
        </div>
      </div>
      <div className="actions-row">
        <button className="btn primary" onClick={()=>onSave({summary,business,assignee:assignee||null,priority,dueAt:dueAt||null})}>
          Save & approve
        </button>
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

/* ───────────────────────────── bulk bar ────────────────────────────────── */

function BulkBar({count, items, onApprove, onReject, onSnooze, onClear}) {
  if (count === 0) return null;
  // group by action
  const groups = {};
  items.forEach(x => { groups[x.action] = (groups[x.action]||0) + 1; });
  return (
    <div className={"bulkbar show"}>
      <div className="bb-count"><span className="acc">{count}</span> selected</div>
      <span className="bb-grp">
        {Object.entries(groups).map(([a,n],i,arr)=>(
          <span key={a}>
            <span className={"action " + actionClass(a)} style={{fontSize:10,marginRight:3}}>{a.replace("_TASK","")}</span>
            <span style={{fontVariantNumeric:"tabular-nums"}}>{n}</span>
            {i < arr.length-1 ? <span style={{color:"var(--fg-faint)",margin:"0 6px"}}>·</span> : null}
          </span>
        ))}
      </span>
      <button className="btn primary" onClick={onApprove}>
        <span className="pill-kbd">1</span> Approve {count}
      </button>
      <button className="btn danger" onClick={onReject}>
        <span className="pill-kbd">2</span> Reject…
      </button>
      <button className="btn" onClick={onSnooze}>
        <span className="pill-kbd">H</span> Snooze…
      </button>
      <button className="btn ghost" onClick={onClear}>
        <span className="pill-kbd">Esc</span> Clear
      </button>
    </div>
  );
}

/* ───────────────────────────── modals ─────────────────────────────────── */

function RejectModal({ids, items, onCancel, onSubmit}) {
  const [reason, setReason] = useState("");
  const presets = ["Wrong","Duplicate","Out of scope","Already handled","Spam / bot"];
  const groups = {};
  items.forEach(x => { groups[x.action] = (groups[x.action]||0) + 1; });
  return (
    <div className="modal-wrap" onClick={onCancel}>
      <div className="modal" onClick={e=>e.stopPropagation()}>
        <div className="modal-h">
          <span>Reject {ids.length} {ids.length===1?"candidate":"candidates"}</span>
          <span className="small">shared reason</span>
        </div>
        <div className="modal-b">
          <div>
            <div className="field-lbl">Reason</div>
            <div className="reasons" style={{marginBottom:8}}>
              {presets.map(p=>(
                <span key={p} className={"reason" + (reason===p?" on":"")} onClick={()=>setReason(p)}>{p}</span>
              ))}
            </div>
            <textarea value={reason} onChange={e=>setReason(e.target.value)} placeholder="Optional detail — applied to all selected" autoFocus></textarea>
          </div>
          <div style={{fontSize:11.5,color:"var(--fg-mut)",display:"flex",gap:8,flexWrap:"wrap"}}>
            <span style={{textTransform:"uppercase",letterSpacing:".05em"}}>Affecting:</span>
            {Object.entries(groups).map(([a,n])=>(
              <span key={a}>
                <span className={"action " + actionClass(a)} style={{fontSize:10}}>{a.replace("_TASK","")}</span> {n}
              </span>
            ))}
          </div>
        </div>
        <div className="modal-f">
          <button className="btn ghost" onClick={onCancel}>Cancel</button>
          <button className="btn danger" style={{background:"var(--c-redhot-bg)",borderColor:"var(--c-redhot-bg)"}} onClick={()=>onSubmit(reason||"—")}>
            Reject {ids.length}
          </button>
        </div>
      </div>
    </div>
  );
}

function SnoozeModal({ids, items, onCancel, onSubmit}) {
  const [pick, setPick] = useState("tomorrow");
  const [custom, setCustom] = useState("");
  const opts = [
    ["3h",      "+3h",  "this afternoon"],
    ["tomorrow","Tom.", "tomorrow 9am"],
    ["weekend", "Sat",  "this Saturday"],
    ["next-mon","Mon",  "next Monday"],
  ];
  const submit = () => {
    if (pick === "custom" && custom) onSubmit(custom);
    else onSubmit(opts.find(o=>o[0]===pick)?.[2] || "later");
  };
  return (
    <div className="modal-wrap" onClick={onCancel}>
      <div className="modal" onClick={e=>e.stopPropagation()}>
        <div className="modal-h">
          <span>Snooze {ids.length} {ids.length===1?"candidate":"candidates"}</span>
          <span className="small">hidden from Inbox until then</span>
        </div>
        <div className="modal-b">
          <div className="quickdates">
            {opts.map(([k,t,sub]) => (
              <div key={k} className={"qd" + (pick===k?" on":"")} onClick={()=>setPick(k)}>
                {t}<small>{sub}</small>
              </div>
            ))}
          </div>
          <div>
            <div className="field-lbl">Or custom date</div>
            <input type="text" placeholder="e.g. May 20" value={custom}
                   onChange={e=>{setCustom(e.target.value);setPick("custom");}} />
          </div>
        </div>
        <div className="modal-f">
          <button className="btn ghost" onClick={onCancel}>Cancel</button>
          <button className="btn primary" onClick={submit}>Snooze {ids.length}</button>
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────────── help overlay ────────────────────────────── */

function Help({onClose}) {
  const Kbd = ({k}) => <span className="pill-kbd">{k}</span>;
  const Row = ({label, ks}) => (
    <div className="help-row">
      <span>{label}</span>
      <span className="keys">{ks.map((k,i)=>(<Kbd k={k} key={i} />))}</span>
    </div>
  );
  return (
    <div className="help">
      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:6}}>
        <div style={{fontWeight:600,fontSize:12.5}}>Keyboard</div>
        <button className="iconbtn" onClick={onClose} style={{width:22,height:22}}>
          <Icon.X className="source-icon" />
        </button>
      </div>
      <div className="help-h">Navigation</div>
      <Row label="Move focus" ks={["J","K"]} />
      <Row label="Open / expand" ks={["↵"]} />
      <Row label="Toggle selection" ks={["X"]} />
      <Row label="Extend selection" ks={["⇧J"]} />
      <Row label="Select all visible" ks={["⌘A"]} />
      <Row label="Clear / collapse" ks={["Esc"]} />
      <div className="help-h">Triage actions</div>
      <Row label="Approve" ks={["1"]} />
      <Row label="Reject…" ks={["2"]} />
      <Row label="Edit + approve" ks={["3"]} />
      <Row label="Snooze…" ks={["H"]} />
      <div className="help-h">Global</div>
      <Row label="Command / search" ks={["⌘K"]} />
      <Row label="Filter rows" ks={["/"]} />
      <Row label="Go to Triage" ks={["G","R"]} />
      <Row label="Go to Tasks" ks={["G","T"]} />
      <Row label="Go to SOPs" ks={["G","S"]} />
      <Row label="This panel" ks={["?"]} />
    </div>
  );
}

/* mount */
ReactDOM.createRoot(document.getElementById("root")).render(<App />);
