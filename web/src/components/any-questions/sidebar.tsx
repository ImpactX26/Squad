"use client";

// The persistent right-hand "Any Questions" sidebar (ARCHITECTURE.md §11.2). On a wide screen it sits
// beside the page and is resized by dragging its left edge (or with the arrow keys on that edge); below
// SIDE_BY_SIDE it slides over the page as a sheet. Closed, it stays mounted but inert, so a conversation
// survives being hidden and nothing inside it can take focus.

import { type PointerEvent as ReactPointerEvent, useEffect, useRef, useState } from "react";

import { AnyQuestionsChat } from "@/components/any-questions/chat";
import { PANEL_DEFAULT, PANEL_MAX, PANEL_MIN, SIDE_BY_SIDE, useAnyQuestions } from "@/lib/any-questions";

const KEY_STEP = 24;

export function AnyQuestionsSidebar() {
  const { available, open, setOpen, width, setWidth } = useAnyQuestions();
  const [dragging, setDragging] = useState(false);
  const [live, setLive] = useState<number | null>(null); // the width while a drag is under way
  const start = useRef({ x: 0, width: 0 });

  // A sheet on a small screen closes on Escape, like any other overlay.
  useEffect(() => {
    if (!open) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape" && window.innerWidth < SIDE_BY_SIDE) setOpen(false);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, setOpen]);

  if (!available) return null;

  const shown = live ?? width;

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    start.current = { x: event.clientX, width };
    setDragging(true);
    setLive(width);
  }

  function onPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    if (!dragging) return;
    // Dragging the edge left makes the panel wider.
    const next = start.current.width + (start.current.x - event.clientX);
    setLive(Math.min(Math.max(next, PANEL_MIN), Math.min(PANEL_MAX, window.innerWidth * 0.6)));
  }

  function onPointerUp() {
    if (!dragging) return;
    setDragging(false);
    if (live !== null) setWidth(live);
    setLive(null);
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const moves: Record<string, number> = { ArrowLeft: KEY_STEP, ArrowRight: -KEY_STEP };
    if (event.key in moves) {
      event.preventDefault();
      setWidth(width + moves[event.key]);
    } else if (event.key === "Home") {
      event.preventDefault();
      setWidth(PANEL_MIN);
    } else if (event.key === "End") {
      event.preventDefault();
      setWidth(PANEL_MAX);
    }
  }

  return (
    <>
      {/* The sheet's backdrop, small screens only. */}
      <div
        aria-hidden
        onClick={() => setOpen(false)}
        className={`fixed inset-0 top-14 z-30 bg-ink/30 backdrop-blur-[2px] transition-opacity duration-300 lg:hidden ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />
      <aside
        id="any-questions"
        aria-label="Any Questions"
        inert={!open}
        // The column animates to 0 when closed; the panel inside keeps its width, so it slides out whole.
        style={{ "--aq-width": `${open ? shown : 0}px`, "--aq-inner": `${shown}px` } as React.CSSProperties}
        className={[
          // Small screens: a sheet from the right.
          "fixed top-14 right-0 bottom-0 z-40 w-full max-w-md bg-surface lg:bg-transparent",
          "transition-[transform,box-shadow] duration-300 ease-[cubic-bezier(0.2,0.7,0.2,1)]",
          open ? "translate-x-0 shadow-raised" : "translate-x-full shadow-none",
          // Wide screens: a column beside the page, as wide as the person made it.
          "lg:sticky lg:z-20 lg:h-[calc(100dvh-3.5rem)] lg:max-w-none lg:w-(--aq-width) lg:shrink-0 lg:translate-x-0 lg:self-start lg:overflow-hidden lg:shadow-none",
          dragging ? "" : "lg:transition-[width] lg:duration-300",
        ].join(" ")}
      >
        <div className="relative flex h-full flex-col border-l border-white/60 bg-frost dark:border-white/8 backdrop-blur-xl lg:w-(--aq-inner)">
          <div
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize Any Questions"
            aria-controls="any-questions"
            aria-valuemin={PANEL_MIN}
            aria-valuemax={PANEL_MAX}
            aria-valuenow={Math.round(shown)}
            tabIndex={0}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onKeyDown={onKeyDown}
            onDoubleClick={() => setWidth(PANEL_DEFAULT)}
            title="Drag to resize, double-click to reset"
            className="group absolute inset-y-0 -left-2 z-10 hidden w-4 cursor-col-resize touch-none outline-none lg:block"
          >
            <span
              className={`absolute inset-y-0 left-1/2 w-0.5 -translate-x-1/2 transition-colors ${
                dragging ? "bg-accent" : "bg-transparent group-hover:bg-accent/60 group-focus-visible:bg-accent"
              }`}
            />
            <span
              className={`absolute top-1/2 left-1/2 grid h-10 w-2.5 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full bg-surface shadow-card transition ${
                dragging ? "scale-110 opacity-100" : "opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100"
              }`}
            >
              <span className="h-4 w-0.5 rounded-full bg-accent" />
            </span>
          </div>
          <AnyQuestionsChat />
        </div>
      </aside>
      {/* While dragging, the whole page shows the resize cursor and text can't be selected by accident. */}
      {dragging ? <style>{"body{cursor:col-resize;user-select:none}"}</style> : null}
    </>
  );
}
