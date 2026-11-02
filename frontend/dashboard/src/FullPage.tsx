import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft } from "lucide-react";

export function FullPage({ title, onClose, children }: { title: string; onClose: () => void | boolean; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  const closeRef = useRef(onClose);
  const mounted = useRef(false);
  closeRef.current = onClose;
  const id = useId();
  useEffect(() => {
    mounted.current = true;
    const focus = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    if (window.history.state?.arsenalPage) {
      window.history.replaceState({ ...window.history.state, arsenalPage: id }, "");
    } else {
      window.history.pushState({ ...window.history.state, arsenalPage: id }, "");
    }
    const back = () => {
      if (window.history.state?.arsenalPage !== id && closeRef.current() === false) {
        window.history.pushState({ ...window.history.state, arsenalPage: id }, "");
      }
    };
    window.addEventListener("popstate", back);
    ref.current?.showModal();
    return () => {
      window.removeEventListener("popstate", back);
      mounted.current = false;
      // Defer navigation so StrictMode's effect replay does not pop the page.
      queueMicrotask(() => {
        if (!mounted.current && window.history.state?.arsenalPage === id) window.history.back();
      });
      document.body.style.overflow = overflow;
      focus?.focus({ preventScroll: true });
    };
  }, [id]);
  function close() {
    onClose();
  }
  return createPortal(<dialog ref={ref} className="fullPage" aria-label={title} onCancel={(event) => { event.preventDefault(); close(); }}>
    <header className="fullPageHeader">
      <button type="button" className="iconButton" onClick={close} title="Back" aria-label="Back"><ArrowLeft size={22} /></button>
      <h1>{title}</h1>
    </header>
    <div className="fullPageContent">{children}</div>
  </dialog>, document.body);
}
