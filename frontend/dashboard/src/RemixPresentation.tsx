import { useEffect, useRef, useState } from "react";
import { Copy, Download, Plus, Share2, Unlink } from "lucide-react";
import { FullPage } from "./FullPage";
import { apiUrl } from "./catalog";
import { asPublicShareState, apiErrorMessage } from "./api";
import type { CatalogItem, PublicShareState } from "./types";

type PresentationProps = {
  apiBase: string;
  card: boolean;
  onClose: () => void;
  onShareChange?: (id: number, share: PublicShareState) => void;
} & ({ item: CatalogItem; draft?: never } | { item?: never; draft: { id: string; image: string; saving: boolean; error?: string; onSave: () => void } });

function RecipePage({ url }: { url: string }) {
  const frame = useRef<HTMLIFrameElement>(null);
  const observer = useRef<ResizeObserver | null>(null);
  const [height, setHeight] = useState(600);
  const [failed, setFailed] = useState(false);
  useEffect(() => () => observer.current?.disconnect(), []);

  function loaded() {
    observer.current?.disconnect();
    const element = frame.current;
    const document = element?.contentDocument;
    const body = document?.body;
    if (!body?.querySelector('.recipePage')) {
      setFailed(true);
      return;
    }
    if (!element || !document) return;
    // Keep the embedded canvas opaque and let the surrounding page own scrolling.
    const background = getComputedStyle(element).backgroundColor;
    document.documentElement.style.backgroundColor = background;
    document.documentElement.style.overflow = "hidden";
    body.style.backgroundColor = background;
    body.style.overflow = "hidden";
    const resize = () => setHeight(Math.ceil(body.getBoundingClientRect().height));
    observer.current = new ResizeObserver(resize);
    observer.current.observe(body);
    resize();
  }

  return failed ? <p role="alert">Could not load the remix page. Close and reopen to retry.</p> :
    <iframe ref={frame} className="recipePreview" title="Remix recipe" src={url} style={{ height }} onLoad={loaded} allowFullScreen />;
}

export function RemixPresentation({ item, draft, apiBase, card, onClose, onShareChange }: PresentationProps) {
  const linkRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [link, setLink] = useState("");
  const [checking, setChecking] = useState(Boolean(item));
  const [activeShare, setActiveShare] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
  const image = draft ? draft.image : card ? apiUrl(apiBase, `/items/${item.id}/remix-card`) : item.original_path || item.preview_path || "";
  const filename = `remix-${item?.id || draft?.id}.png`;

  useEffect(() => {
    if (!item) return;
    const controller = new AbortController();
    setChecking(true);
    void fetch(apiUrl(apiBase, `/items/${item.id}/share`, { card: card || undefined }), { signal: controller.signal })
      .then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new Error(apiErrorMessage(payload, response.status));
        if (controller.signal.aborted) return;
        const state = asPublicShareState(payload);
        setActiveShare(state.active);
        setEnabled(state.enabled === true);
        setLink(state.active ? (card ? state.card_url : state.url) || "" : "");
      }).catch((error) => { if (!controller.signal.aborted) setNotice(error.message); })
      .finally(() => { if (!controller.signal.aborted) setChecking(false); });
    return () => controller.abort();
  }, [apiBase, item?.id, card]);

  async function copyLink() {
    try { await navigator.clipboard.writeText(link); setNotice("Link copied"); }
    catch { linkRef.current?.select(); setNotice("Select and copy the link"); }
  }

  async function revoke() {
    if (!item || busy) return;
    setBusy(true);
    try {
      const response = await fetch(apiUrl(apiBase, `/items/${item.id}/share`), { method: "DELETE" });
      const payload = await response.json();
      if (!response.ok) throw new Error(apiErrorMessage(payload, response.status));
      setLink("");
      setActiveShare(false);
      setNotice("All links for this item revoked");
      onShareChange?.(item.id, asPublicShareState(payload));
    } catch (error) { setNotice(error instanceof Error ? error.message : "Could not revoke share"); }
    finally { setBusy(false); }
  }

  async function share() {
    if (busy) return;
    setBusy(true);
    setNotice("");
    try {
      if (draft) {
        draft.onSave();
        return;
      }
      if (!item) return;
      let url = link;
      if (!url) {
        const response = await fetch(apiUrl(apiBase, `/items/${item.id}/share`, { card: card || undefined }), { method: "POST" });
        const payload = await response.json();
        if (!response.ok) throw new Error(apiErrorMessage(payload, response.status));
        const state = asPublicShareState(payload);
        url = (card ? state.card_url : state.url) || "";
        if (!state.active || !state.url) throw new Error("Could not prepare public link");
        setLink(url);
        setActiveShare(true);
        onShareChange?.(item.id, state);
      }
      if (navigator.share) {
        try {
          await navigator.share({ title: card ? "Forward Deployed Meme | Remix" : item.summary || "Remix", url });
          setNotice("Shared");
          return;
        } catch (error) {
          if (error instanceof Error && error.name === "AbortError") return;
        }
      }
      try {
        await navigator.clipboard.writeText(url);
        setNotice("Public link copied");
      } catch {
        setNotice("Public link ready");
      }
    } catch (error) {
      if (!(error instanceof Error && error.name === "AbortError")) setNotice(error instanceof Error ? error.message : "Could not share. Please retry.");
    } finally {
      setBusy(false);
    }
  }

  return <FullPage title={card ? "Remix Page" : "Share Remix"} onClose={onClose}>
    <div className={`remixPresentation${card ? " recipePresentation" : ""}`}>
    <div className="presentationMedia">
      {card ? <RecipePage url={`${image}?page=true`} /> : imageFailed ? <p role="alert">Could not load the image. Close and reopen to retry.</p> : <img src={image} alt={item?.summary || "Generated remix"} onError={() => setImageFailed(true)} />}
    </div>
    <aside className="presentationControls"><div className="presentationActions">
      {!card ? <a className="secondaryAction" href={image} download={filename}><Download size={17} />Download</a> : null}
      <button className="primaryButton" type="button" disabled={checking || busy || Boolean(draft?.saving) || (!draft && !enabled)} onClick={share}>{draft ? <Plus size={17} /> : <Share2 size={17} />}{draft ? draft.saving ? "Saving" : "Save to library" : busy ? "Preparing" : card ? activeShare ? "Share page" : "Publish page" : "Share Media"}</button>
    </div>
    {link ? <div className="shareLinkBox"><input ref={linkRef} aria-label="Public share link" readOnly value={link} onFocus={(event) => event.currentTarget.select()} /><button className="iconButton" type="button" title="Copy link" aria-label="Copy link" onClick={copyLink}><Copy size={18} /></button></div> : null}
    {activeShare ? <button className="secondaryAction shareRevoke" type="button" disabled={busy || checking} onClick={revoke}><Unlink size={17} />Revoke shares</button> : null}
    {!draft && !checking && !enabled ? <p>Public sharing is disabled</p> : null}
    {card && !draft ? <p>Publishing includes eligible saved source previews, descriptions and permitted video presentation. Summaries may update. Recall this item's shares to withdraw recipe consent.</p> : null}
    {notice ? <p className="presentationNotice" role="status">{notice}</p> : null}
    {draft?.error ? <p className="presentationNotice" role="alert">{draft.error}</p> : null}
    </aside></div>
  </FullPage>;
}
