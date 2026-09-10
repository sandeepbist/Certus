export type PdfRightAngle = 0 | 90 | 180 | 270;

export function rotateCropboxPoint(
  x: number,
  y: number,
  width: number,
  height: number,
  rotation: PdfRightAngle,
) {
  if (rotation === 90) return [height - y, x] as const;
  if (rotation === 180) return [width - x, height - y] as const;
  if (rotation === 270) return [y, width - x] as const;
  return [x, y] as const;
}
