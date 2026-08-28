export type Layout = "mobile" | "desktop";

const MQ_NARROW = "(max-width: 900px)";
const MQ_COARSE = "(pointer: coarse)";
const MQ_COARSE_WIDE = "(max-width: 1100px)";

export function detectLayout(): Layout {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return "desktop";
  }
  const narrow = window.matchMedia(MQ_NARROW).matches;
  const coarsePad =
    window.matchMedia(MQ_COARSE).matches && window.matchMedia(MQ_COARSE_WIDE).matches;
  return narrow || coarsePad ? "mobile" : "desktop";
}

export function applyLayout(layout: Layout) {
  const html = document.documentElement;
  const prev = html.getAttribute("data-layout");
  html.setAttribute("data-layout", layout);
  if (prev !== layout) {
    requestAnimationFrame(() => {
      window.dispatchEvent(new Event("resize"));
    });
  }
}

export function subscribeLayout(cb: (layout: Layout) => void): () => void {
  const sync = () => {
    const layout = detectLayout();
    applyLayout(layout);
    cb(layout);
  };
  sync();
  window.addEventListener("resize", sync);
  const onOrientation = () => {
    sync();
    window.setTimeout(sync, 80);
    window.setTimeout(sync, 240);
  };
  window.addEventListener("orientationchange", onOrientation);
  const mqs = [MQ_NARROW, MQ_COARSE, MQ_COARSE_WIDE].map((q) => window.matchMedia(q));
  for (const mq of mqs) {
    mq.addEventListener("change", sync);
  }
  return () => {
    window.removeEventListener("resize", sync);
    window.removeEventListener("orientationchange", onOrientation);
    for (const mq of mqs) mq.removeEventListener("change", sync);
  };
}
