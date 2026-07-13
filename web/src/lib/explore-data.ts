export interface ExplorePoints {
  count: number;
  x: Float32Array;
  y: Float32Array;
  clusterT0: Int16Array;
  clusterT1: Int16Array;
  clusterT2: Int16Array;
  clusterT3: Int16Array;
  contentType: Uint8Array;
}

export function parsePointsBinary(buffer: ArrayBuffer): ExplorePoints {
  const view = new DataView(buffer);
  const count = view.getUint32(0, true);

  const x = new Float32Array(count);
  const y = new Float32Array(count);
  const clusterT0 = new Int16Array(count);
  const clusterT1 = new Int16Array(count);
  const clusterT2 = new Int16Array(count);
  const clusterT3 = new Int16Array(count);
  const contentType = new Uint8Array(count);

  for (let i = 0; i < count; i++) {
    const offset = 4 + i * 17;
    x[i] = view.getFloat32(offset, true);
    y[i] = view.getFloat32(offset + 4, true);
    clusterT0[i] = view.getInt16(offset + 8, true);
    clusterT1[i] = view.getInt16(offset + 10, true);
    clusterT2[i] = view.getInt16(offset + 12, true);
    clusterT3[i] = view.getInt16(offset + 14, true);
    contentType[i] = view.getUint8(offset + 16);
  }

  return { count, x, y, clusterT0, clusterT1, clusterT2, clusterT3, contentType };
}

export function parseDatesBinary(
  buffer: ArrayBuffer
): { count: number; maxOffset: number; offsets: Uint16Array } {
  const view = new DataView(buffer);
  const count = view.getUint32(0, true);
  const maxOffset = view.getUint32(4, true);
  const offsets = new Uint16Array(count);
  for (let i = 0; i < count; i++) {
    offsets[i] = view.getUint16(8 + i * 2, true);
  }
  return { count, maxOffset, offsets };
}

export interface TimelinePapers {
  lccn: string;
  title: string;
}

export interface TimelineData {
  count: number;
  papers: TimelinePapers[];
  paperIdx: Uint16Array;
  dateOffset: Uint16Array;
  clusterT0: Int16Array;
  clusterT1: Int16Array;
  clusterT2: Int16Array;
  clusterT3: Int16Array;
  quality: Uint8Array;
  contentType: Uint8Array;
}

export function parseTimelineBinary(buffer: ArrayBuffer): TimelineData {
  const view = new DataView(buffer);
  const count = view.getUint32(0, true);
  const papersByteLen = view.getUint32(4, true);

  const papersText = new TextDecoder().decode(
    new Uint8Array(buffer, 8, papersByteLen)
  );
  const papers: TimelinePapers[] = [];
  for (const line of papersText.split("\n")) {
    if (!line) continue;
    const [lccn, title] = line.split("\t");
    if (lccn) papers.push({ lccn, title: title ?? lccn });
  }

  const offset0 = 8 + papersByteLen;
  const paperIdx = new Uint16Array(count);
  const dateOffset = new Uint16Array(count);
  const clusterT0 = new Int16Array(count);
  const clusterT1 = new Int16Array(count);
  const clusterT2 = new Int16Array(count);
  const clusterT3 = new Int16Array(count);
  const quality = new Uint8Array(count);
  const contentType = new Uint8Array(count);

  for (let i = 0; i < count; i++) {
    const o = offset0 + i * 14;
    paperIdx[i] = view.getUint16(o, true);
    dateOffset[i] = view.getUint16(o + 2, true);
    clusterT0[i] = view.getInt16(o + 4, true);
    clusterT1[i] = view.getInt16(o + 6, true);
    clusterT2[i] = view.getInt16(o + 8, true);
    clusterT3[i] = view.getInt16(o + 10, true);
    quality[i] = view.getUint8(o + 12);
    contentType[i] = view.getUint8(o + 13);
  }

  return {
    count, papers, paperIdx, dateOffset,
    clusterT0, clusterT1, clusterT2, clusterT3,
    quality, contentType,
  };
}

export interface ClusterInfo {
  id: string;
  label: number;
  size: number;
  date_min: string | null;
  date_max: string | null;
  parent_id: string | null;
  label_text: string | null;
}

export interface ChunkDetail {
  chunk_id: string;
  content: string;
  paper_title: string;
  paper_lccn: string;
  date_issued: string;
  edition: number;
  page_sequence: number;
  image_url: string;
  cluster_labels: number[];
  content_type: number;
}

const CONTENT_TYPE_LABELS = ["Content", "Ad", "Legal", "Bad OCR"] as const;
export function contentTypeLabel(t: number): string {
  return CONTENT_TYPE_LABELS[t] ?? `Unknown (${t})`;
}

const TIER_COLORS: [number, number, number][] = [
  [31, 119, 180],
  [255, 127, 14],
  [44, 160, 44],
  [214, 39, 40],
  [148, 103, 189],
  [140, 86, 75],
  [227, 119, 194],
  [188, 189, 34],
  [23, 190, 207],
  [255, 187, 120],
  [152, 223, 138],
  [255, 152, 150],
  [197, 176, 213],
  [196, 156, 148],
  [247, 182, 210],
  [199, 199, 199],
  [219, 219, 141],
  [158, 218, 229],
  [174, 199, 232],
  [255, 215, 0],
  [0, 200, 200],
  [255, 105, 180],
  [100, 200, 100],
  [200, 100, 200],
  [255, 165, 0],
];

const OUTLIER_COLOR: [number, number, number] = [70, 70, 70];

export function clusterColor(label: number): [number, number, number] {
  if (label < 0) return OUTLIER_COLOR;
  return TIER_COLORS[label % TIER_COLORS.length];
}

export function isOutlier(label: number): boolean {
  return label < 0;
}
