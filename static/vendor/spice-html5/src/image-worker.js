"use strict";

/* Decode CPU-heavy SPICE images away from input handling and canvas painting.
   The decoder modules only need createImageData; pixels are transferred back
   without copying the decoded buffer a second time. */
import { convert_spice_quic_to_web } from './quic.js';
import { convert_spice_lz_to_web } from './lz.js';
import { convert_spice_bitmap_to_web } from './bitmap.js';

const context = {
    createImageData(width, height)
    {
        if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) ||
            width <= 0 || height <= 0 || width * height * 4 > 128 * 1024 * 1024)
            throw new Error('SPICE image exceeds decode limit');
        return { width, height, data: new Uint8ClampedArray(width * height * 4) };
    },
};

self.onmessage = function(event)
{
    const { id, kind, source } = event.data;
    try
    {
        const image = kind === 'quic' ? convert_spice_quic_to_web(context, source) :
                      kind === 'lz' ? convert_spice_lz_to_web(context, source) :
                      convert_spice_bitmap_to_web(context, source);
        if (!image)
            throw new Error('Unsupported SPICE image format');
        self.postMessage({ id, width: image.width, height: image.height,
                           pixels: image.data.buffer, type: source.type },
                         [image.data.buffer]);
    }
    catch (error)
    {
        self.postMessage({ id, error: String(error) });
    }
};
