import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";

const KEY = "sidebarWidth";
export const DEFAULT_WIDTH = 300;
const MIN = 200;
const MAX = 640;
const MIN_CHAT = 360; // the chat keeps at least this much room

function clamp(w: number): number {
  const max = Math.max(MIN, Math.min(MAX, window.innerWidth - MIN_CHAT));
  return Math.round(Math.min(max, Math.max(MIN, w)));
}

/** The sidebar width, remembered in this browser (a convenience: falls back to
 *  the default if storage is unavailable). */
export function useSidebarWidth(): [number, (w: number) => void] {
  const [width, setWidth] = useState(() => {
    try {
      const saved = Number(localStorage.getItem(KEY));
      return saved ? clamp(saved) : DEFAULT_WIDTH;
    } catch {
      return DEFAULT_WIDTH;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(KEY, String(width));
    } catch {
      /* storage blocked: the width just isn't remembered */
    }
  }, [width]);
  return [width, (w) => setWidth(clamp(w))];
}

/** Drag handle on the sidebar's right edge. Also works from the keyboard
 *  (arrow keys; Home resets) and by double-click (reset). */
export default function SidebarResizer({ width, onResize }: { width: number; onResize: (w: number) => void }) {
  const origin = useRef<number | null>(null); // layout's left edge while dragging
  const [dragging, setDragging] = useState(false);

  const down = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    origin.current = e.currentTarget.parentElement?.getBoundingClientRect().left ?? 0;
    e.currentTarget.setPointerCapture(e.pointerId);
    setDragging(true);
    e.preventDefault();
  };
  const move = (e: PointerEvent<HTMLDivElement>) => {
    if (origin.current !== null) onResize(e.clientX - origin.current);
  };
  const up = () => {
    origin.current = null;
    setDragging(false);
  };
  const key = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 64 : 16;
    if (e.key === "ArrowLeft") onResize(width - step);
    else if (e.key === "ArrowRight") onResize(width + step);
    else if (e.key === "Home") onResize(DEFAULT_WIDTH);
    else return;
    e.preventDefault();
  };

  return (
    <div
      className={dragging ? "sidebar-resizer dragging" : "sidebar-resizer"}
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize the documents sidebar"
      aria-valuenow={width}
      aria-valuemin={MIN}
      aria-valuemax={MAX}
      tabIndex={0}
      title="Drag to resize · double-click to reset"
      onPointerDown={down}
      onPointerMove={move}
      onPointerUp={up}
      onPointerCancel={up}
      onDoubleClick={() => onResize(DEFAULT_WIDTH)}
      onKeyDown={key}
    />
  );
}
