import { useState, type FormEvent } from "react";
import { Save } from "lucide-react";
import { FullPage } from "./FullPage";
import { apiUrl } from "./catalog";
import { asCatalogItem, apiErrorMessage } from "./api";
import type { CatalogItem } from "./types";

const tagFields = ["keywords", "topics", "people_or_characters", "tone", "reaction_type"] as const;

export function MetadataEditor({ item, apiBase, onClose, onSave }: { item: CatalogItem; apiBase: string; onClose: () => void; onSave: (item: CatalogItem) => void }) {
  const [values, setValues] = useState<Record<string, string>>(() => ({
    summary: item.summary || "", visible_text: item.visible_text || "", narration: item.narration || "",
    language: item.language || "", meme_format: item.meme_format || "",
    ...Object.fromEntries(tagFields.map((field) => [field, (item[field] || []).join(", ")])),
  }));
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [dirty, setDirty] = useState(false);
  function close() {
    if (busy || (dirty && !window.confirm("Discard unsaved changes?"))) return false;
    onClose();
    return true;
  }
  function change(field: string, value: string) {
    setValues((current) => ({ ...current, [field]: value }));
    setDirty(true);
    setNotice("");
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      const body = { ...values, ...Object.fromEntries(tagFields.map((field) => [field, values[field].split(",").map((tag) => tag.trim()).filter(Boolean)])) };
      const response = await fetch(apiUrl(apiBase, `/items/${item.id}/metadata`), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const payload = await response.json();
      if (!response.ok) throw new Error(apiErrorMessage(payload, response.status));
      onSave(asCatalogItem(payload));
      setDirty(false);
      setNotice("Saved");
    } catch (error) { setNotice(error instanceof Error ? error.message : "Could not save changes"); }
    finally { setBusy(false); }
  }
  const fields = [
    ["summary", "Description", "4000"], ["visible_text", "Visible text", "16000"], ["narration", "Narration", "16000"],
    ["keywords", "Tags", ""], ["topics", "Topics", ""], ["people_or_characters", "People and characters", ""],
    ["tone", "Tone", ""], ["reaction_type", "Reactions", ""], ["language", "Language", "64"], ["meme_format", "Format", "128"],
  ];
  return <FullPage title="Edit meme" onClose={close}><div className="metadataEditor">
    <div className="editorMedia">{item.media_type === "video" ? <video src={item.playback_path || item.original_path || ""} poster={item.preview_path || undefined} controls playsInline disablePictureInPicture /> : <img src={item.preview_path || item.original_path || ""} alt={item.summary || "Meme"} />}</div>
    <form onSubmit={save} className="metadataForm">
      {fields.map(([field, label, limit], index) => <label className={index < 3 ? "wideField" : ""} key={field}><span>{label}</span>
        {index < 3 ? <textarea rows={field === "summary" ? 3 : 5} maxLength={Number(limit)} value={values[field]} onChange={(event) => change(field, event.target.value)} disabled={busy} /> : <input value={values[field]} maxLength={limit ? Number(limit) : 12000} onChange={(event) => change(field, event.target.value)} disabled={busy} />}
      </label>)}
      <div className="editorSave"><button type="submit" className="primaryButton" disabled={busy || !dirty}><Save size={18} />{busy ? "Saving" : "Save changes"}</button>{notice ? <span role="status">{notice}</span> : null}</div>
    </form>
  </div></FullPage>;
}
