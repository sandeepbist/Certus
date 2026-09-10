import { describe, expect, test } from 'bun:test';

import { rotateCropboxPoint } from '../../services/web/src/components/documents/pdfGeometry';


describe('PDF cropbox geometry rotation', () => {
  test('maps unrotated top-left points into each PDF.js viewport rotation', () => {
    expect(rotateCropboxPoint(20, 30, 300, 500, 0)).toEqual([20, 30]);
    expect(rotateCropboxPoint(20, 30, 300, 500, 90)).toEqual([470, 20]);
    expect(rotateCropboxPoint(20, 30, 300, 500, 180)).toEqual([280, 470]);
    expect(rotateCropboxPoint(20, 30, 300, 500, 270)).toEqual([30, 280]);
  });

  test('rotates all cropbox corners into the viewport bounds', () => {
    const corners = [[0, 0], [300, 0], [0, 500], [300, 500]] as const;
    for (const rotation of [0, 90, 180, 270] as const) {
      const viewportWidth = rotation % 180 === 0 ? 300 : 500;
      const viewportHeight = rotation % 180 === 0 ? 500 : 300;
      for (const [x, y] of corners) {
        const [rotatedX, rotatedY] = rotateCropboxPoint(x, y, 300, 500, rotation);
        expect(rotatedX).toBeGreaterThanOrEqual(0);
        expect(rotatedX).toBeLessThanOrEqual(viewportWidth);
        expect(rotatedY).toBeGreaterThanOrEqual(0);
        expect(rotatedY).toBeLessThanOrEqual(viewportHeight);
      }
    }
  });
});
