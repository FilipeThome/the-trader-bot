import { useEffect, useId, useRef, useState } from "react";

type HelpProps = {
  text: string;
  align?: "start" | "end";
};

export function Help({ text, align = "start" }: HelpProps) {
  const [hover, setHover] = useState(false);
  const [pinned, setPinned] = useState(false);
  const root = useRef<HTMLSpanElement>(null);
  const tipId = useId();
  const open = hover || pinned;

  useEffect(() => {
    if (!pinned) return;
    function onPointer(event: MouseEvent) {
      if (!root.current?.contains(event.target as Node)) setPinned(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setPinned(false);
    }
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [pinned]);

  return (
    <span
      ref={root}
      className="relative inline-flex align-middle"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <button
        type="button"
        className="ml-1 inline-flex size-4 shrink-0 items-center justify-center rounded-full border border-neutral-400 text-[10px] font-semibold leading-none text-neutral-500 hover:border-neutral-800 hover:text-neutral-900 dark:border-neutral-500 dark:hover:border-neutral-200 dark:hover:text-neutral-100"
        aria-label={text}
        aria-expanded={open}
        aria-describedby={open ? tipId : undefined}
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          setPinned((value) => !value);
        }}
      >
        <span aria-hidden="true">?</span>
      </button>
      {open && (
        <span
          id={tipId}
          role="tooltip"
          className={`absolute top-full z-50 mt-1 w-64 rounded-md border border-neutral-200 bg-white p-2 text-left text-xs font-normal normal-case leading-snug text-neutral-800 shadow-lg dark:border-neutral-600 dark:bg-neutral-900 dark:text-neutral-100 ${align === "end" ? "right-0" : "left-0"}`}
        >
          {text}
        </span>
      )}
    </span>
  );
}
