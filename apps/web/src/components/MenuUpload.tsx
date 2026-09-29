"use client";
import { useState } from "react";
import type { MenuSection } from "@/lib/types";

export default function MenuUpload() {
  const [sections, setSections] = useState<MenuSection[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  return <div className="menu-upload">
    <label>Upload menu photo or PDF
      <input type="file" accept="image/jpeg,image/png,application/pdf" disabled={busy}
        onChange={async (event) => {
          const file = event.target.files?.[0];
          if (!file) return;
          event.target.value = "";
          setSections([]);
          if (file.size > 3 * 1024 * 1024) { setMessage("Please choose a file smaller than 3 MB."); return; }
          setBusy(true); setMessage("Reading your menu…");
          try {
            const response = await fetch("/api/menu-upload", {
              method: "POST", headers: { "Content-Type": file.type }, body: file,
              signal: AbortSignal.timeout(120000),
            });
            const body = await response.json();
            if (!response.ok) throw new Error(body.detail || "Could not read the menu.");
            setSections(body.sections);
            setMessage(body.sections.length ? "Uploaded menu — ingredients and allergens are unverified. Confirm with staff." : "No individual dishes found. Try a clearer image.");
          } catch { setMessage("Could not read this menu. Try a clearer JPEG, PNG or PDF, or scan the restaurant website."); }
          finally { setBusy(false); }
        }} />
    </label>
    <small>JPEG, PNG or PDF · up to 3 MB. Sent to the menu reader; not saved as the restaurant’s official menu.</small>
    <p role="status">{message}</p>
    {sections.map((section, index) => <section key={index}>
      <h4>{section.title}</h4>
      {section.items.map((item, i) => <p key={i}><strong>{item.name}</strong> · {item.price || "Price not listed"}</p>)}
    </section>)}
  </div>;
}
