import type { CSSProperties } from "react";
const paths: Record<string, string[]> = {
  facts: ["M5 4h14v16H5z", "M8 8h8M8 12h5M8 16h3"],
  overview: ["M3 3h7v7H3z", "M14 3h7v7h-7z", "M3 14h7v7H3z", "M14 14h7v7h-7z"],
  pipeline: ["M12 3v12", "m7 10 5 5 5-5", "M4 15v5h16v-5"],
  chat: ["M21 11a8 8 0 0 1-8 8H8l-5 3 1-7a8 8 0 1 1 17-4Z"],
  documents: ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z", "M14 2v6h6", "M8 13h8M8 17h5"],
  check: ["M9 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-4", "m9 11 3 3L22 4"],
  libraries: ["M3 3h4v18H3z", "M9 3h4v18H9z", "m15 4 4-1 4 17-4 1z"],
  archive: ["M3 7h18v14H3z", "M2 3h20v4H2z", "M9 11h6"],
  sources: ["M3 7v13h18V5H11L9 3H3v4Z"],
  brain: ["M12 3v18", "M8 3a4 4 0 0 0-4 4 4 4 0 0 0-1 7 4 4 0 0 0 5 6h4", "M16 3a4 4 0 0 1 4 4 4 4 0 0 1 1 7 4 4 0 0 1-5 6h-4", "M4 7h3l2 3M20 7h-3l-2 3"],
  settings: ["M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z", "m9 3 1-2h4l1 2 3 2 2 .5 2 3.5-1.5 2v3l1.5 2-2 3.5-2 .5-3 2-1 2h-4l-1-2-3-2-2-.5L2 16l1.5-2v-3L2 9l2-3.5L6 5Z"],
  schemes: ["m3 3 18 18H3Z", "M7 12v5h5", "m14 3 7 7-3 3-7-7Z"],
  knowledge: ["M12 5C8 2 4 3 2 4v16c3-2 7-2 10 0 3-2 7-2 10 0V4c-2-1-6-2-10 1Z", "M12 5v15"],
  registry: ["M4 4h16v16H4z", "M4 9h16M9 9v11"],
  plus: ["M12 5v14M5 12h14"], arrow: ["M5 12h14", "m13 6 6 6-6 6"],
  menu: ["M4 6h16M4 12h16M4 18h16"], close: ["m6 6 12 12M6 18 18 6"],
  chevron: ["m9 5 7 7-7 7"], down: ["m6 9 6 6 6-6"],
  project: ["M3 21h18M5 21V5l7-3v19M12 8h7v13M8 7v1M8 11v1M8 15v1M15 12h1M15 16h1"],
  logout: ["M9 21H4V3h5", "M10 12h11m-4-4 4 4-4 4"],
  mic: ["M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Z", "M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"],
  spark: ["m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"],
  sun: ["M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z", "M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"],
  moon: ["M21 13A9 9 0 0 1 11 3a9 9 0 1 0 10 10Z"],
};
export default function Icon({ name, size = 18, style }: { name: string; size?: number; style?: CSSProperties }) {
  const key = name.startsWith("registry:") ? "registry" : name === "aosr" ? "documents" : name;
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" style={style}>{(paths[key] ?? paths.documents).map((d, i) => <path key={i} d={d} />)}</svg>;
}
