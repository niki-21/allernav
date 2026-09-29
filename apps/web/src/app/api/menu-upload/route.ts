import { NextResponse } from "next/server";
import { buildFastApiUrl } from "@/server/fastapi";
export const runtime = "nodejs";
export const maxDuration = 120;

export async function POST(request: Request) {
  const url = buildFastApiUrl("/api/menu-upload");
  if (!url) return NextResponse.json({ detail: "Menu reader is unavailable." }, { status: 503 });
  const type = request.headers.get("content-type") ?? "";
  if (!["image/jpeg", "image/png", "application/pdf"].includes(type))
    return NextResponse.json({ detail: "Choose a JPEG, PNG or PDF." }, { status: 415 });
  const chunks: Uint8Array[] = [];
  let size = 0;
  const reader = request.body?.getReader();
  if (!reader) return NextResponse.json({ detail: "File is empty." }, { status: 400 });
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > 3 * 1024 * 1024) {
      await reader.cancel();
      return NextResponse.json({ detail: "File exceeds 3 MB." }, { status: 413 });
    }
    chunks.push(value);
  }
  try {
    const response = await fetch(url, {
      method: "POST", headers: { "Content-Type": type }, body: Buffer.concat(chunks),
      signal: AbortSignal.timeout(115000), cache: "no-store",
    });
    if (!response.ok) return NextResponse.json({ detail: "Menu reader could not process this file." }, { status: response.status });
    return NextResponse.json(await response.json());
  } catch {
    return NextResponse.json({ detail: "Menu reader is temporarily unavailable." }, { status: 502 });
  }
}
