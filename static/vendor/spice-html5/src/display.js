"use strict";
/*
   Copyright (C) 2012 by Jeremy P. White <jwhite@codeweavers.com>

   This file is part of spice-html5.

   spice-html5 is free software: you can redistribute it and/or modify
   it under the terms of the GNU Lesser General Public License as published by
   the Free Software Foundation, either version 3 of the License, or
   (at your option) any later version.

   spice-html5 is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU Lesser General Public License for more details.

   You should have received a copy of the GNU Lesser General Public License
   along with spice-html5.  If not, see <http://www.gnu.org/licenses/>.
*/

import * as Webm from './webm.js';
import * as Messages from './spicemsg.js';
import * as Quic from './quic.js';
import * as Utils from './utils.js';
import * as Inputs from './inputs.js';
import { Constants } from './enums.js';
import { SpiceConn } from './spiceconn.js';
import { SpiceRect } from './spicetype.js';
import { convert_spice_lz_to_web } from './lz.js';
import { convert_spice_bitmap_to_web } from './bitmap.js';

/*----------------------------------------------------------------------------
**  FIXME: putImageData  does not support Alpha blending
**           or compositing.  So if we have data in an ImageData
**           format, we have to draw it onto a context,
**           and then use drawImage to put it onto the target,
**           as drawImage does alpha.
**--------------------------------------------------------------------------*/
/* One shared scratch canvas; allocating a fresh one per draw dominated
   profiles under drawing-heavy guests. It only ever grows. */
var scratch_canvas = null;
var scratch_context = null;

/* Draws the src rectangle of d at x,y scaled to dw x dh, blending. */
function putImageDataWithAlpha(context, d, x, y, src, dw, dh)
{
    if (scratch_canvas === null)
    {
        scratch_canvas = document.createElement("canvas");
        scratch_context = scratch_canvas.getContext("2d");
    }
    if (scratch_canvas.width < d.width)
        scratch_canvas.width = d.width;
    if (scratch_canvas.height < d.height)
        scratch_canvas.height = d.height;
    scratch_context.putImageData(d, 0, 0);
    context.drawImage(scratch_canvas, src.left, src.top, src.right - src.left, src.bottom - src.top, x, y, dw, dh);
}

/* The part of an image a draw uses: its src_area, or all of it. */
function source_rect(o, width, height)
{
    if (o.src_area)
        return o.src_area;
    return { left: 0, top: 0, right: width, bottom: height };
}

/* The decoded pixels of an image element, read back through the scratch
   canvas rather than the surface, so a clipped draw can still cache the
   whole image. */
function image_to_image_data(img, width, height)
{
    if (scratch_canvas === null)
    {
        scratch_canvas = document.createElement("canvas");
        scratch_context = scratch_canvas.getContext("2d");
    }
    if (scratch_canvas.width < width)
        scratch_canvas.width = width;
    if (scratch_canvas.height < height)
        scratch_canvas.height = height;
    scratch_context.clearRect(0, 0, width, height);
    scratch_context.drawImage(img, 0, 0);
    return scratch_context.getImageData(0, 0, width, height);
}

/* A VP8 stream paints into a video element over the canvas, out of reach
   of the canvas clip; CSS clip-path carries the same rectangles, relative
   to the element's own origin. A bottom-up stream is shown through a
   scaleY(-1) transform, which flips the clip-path along with the pixels,
   so its rectangles are mirrored here to land where the server put them. */
function apply_video_clip(stream)
{
    if (! stream.video)
        return;
    if (! is_clipped(stream.clip))
    {
        stream.video.style.clipPath = "";
        return;
    }
    var rects = stream.clip.rects.rects || [];
    if (rects.length == 0)
    {
        stream.video.style.clipPath = "inset(100%)";
        return;
    }
    var flipped = ! (stream.flags & Constants.SPICE_STREAM_FLAGS_TOP_DOWN);
    var path = "";
    for (var i = 0; i < rects.length; i++)
    {
        var w = rects[i].right - rects[i].left;
        var h = rects[i].bottom - rects[i].top;
        var x = rects[i].left - stream.dest.left;
        var y = rects[i].top - stream.dest.top;
        if (flipped)
            y = stream.stream_height - y - h;
        path += "M" + x + " " + y + "h" + w + "v" + h + "h" + (-w) + "z";
    }
    stream.video.style.clipPath = 'path("' + path + '")';
}

function is_clipped(clip)
{
    return clip !== undefined && clip.type == Constants.SPICE_CLIP_TYPE_RECTS;
}

/* Runs draw with the context clipped to the rectangles of a
   SPICE_CLIP_TYPE_RECTS clip, the way spice-gtk clips every operation. A
   clip with no rectangles paints nothing. Only path-based drawing honours
   the clip region: putImageData does not, so clipped bitmap draws must go
   through drawImage. */
function with_clip(context, clip, draw)
{
    if (! is_clipped(clip))
    {
        draw();
        return;
    }
    var rects = clip.rects.rects || [];
    if (rects.length == 0)
        return;
    context.save();
    context.beginPath();
    for (var i = 0; i < rects.length; i++)
        context.rect(rects[i].left, rects[i].top,
                     rects[i].right - rects[i].left,
                     rects[i].bottom - rects[i].top);
    context.clip();
    draw();
    context.restore();
}

/*----------------------------------------------------------------------------
**  FIXME: Spice will send an image with '0' alpha when it is intended to
**           go on a surface w/no alpha.  So in that case, we have to strip
**           out the alpha.  The test case for this was flux box; in a Xspice
**           server, right click on the desktop to get the menu; the top bar
**           doesn't paint/highlight correctly w/out this change.
**--------------------------------------------------------------------------*/
function stripAlpha(d)
{
    var i;
    var data = d.data;
    var n = d.width * d.height * 4;
    for (i = 3; i < n; i += 4)
        data[i] = 255;
}

/* putImageData ignores the clip region but takes a dirty rectangle, so an
   opaque clipped draw is one put per clip rectangle, each limited to the
   rectangle's intersection with the image: no blend, no scratch canvas.
   A clip with no rectangles paints nothing, as with_clip does. */
function putImageDataClipped(context, d, x, y, clip, src)
{
    var rects = clip.rects.rects || [];
    var width = src.right - src.left;
    var height = src.bottom - src.top;
    for (var i = 0; i < rects.length; i++)
    {
        var left = Math.max(rects[i].left, x);
        var top = Math.max(rects[i].top, y);
        var right = Math.min(rects[i].right, x + width);
        var bottom = Math.min(rects[i].bottom, y + height);
        if (right > left && bottom > top)
            context.putImageData(d, x - src.left, y - src.top,
                                 src.left + left - x, src.top + top - y, right - left, bottom - top);
    }
}

/* JPEG frames used to be turned into percent-encoded data: URIs one byte at
   a time — an O(n) string build per frame that dominated MJPEG playback.
   A Blob URL hands the bytes to the decoder directly; it must be revoked
   once the image has loaded (or failed) or each frame leaks its blob. */
function jpeg_image_url(data)
{
    return URL.createObjectURL(new Blob([data], { type: "image/jpeg" }));
}

function revoke_jpeg_image_url(img)
{
    if (img.src && img.src.startsWith("blob:"))
        URL.revokeObjectURL(img.src);
}

function handle_draw_jpeg_onerror()
{
    revoke_jpeg_image_url(this);
    if (this.o.sc.streams && this.o.sc.streams[this.o.id])
        this.o.sc.streams[this.o.id].frames_loading--;
    /* An image that will never decode must not hold the queue. */
    this.o.finished = true;
    this.o.sc.mark_ready(this.o.op, null);
}

/*----------------------------------------------------------------------------
**  SpiceDisplayConn
**      Drive the Spice Display Channel
**--------------------------------------------------------------------------*/
function SpiceDisplayConn()
{
    SpiceConn.apply(this, arguments);
    this.monitor_heads = [];
    this.ops = [];
    this.ops_head = 0;
    this.image_jobs = new Map();
    this.image_jobs_bytes = 0;
    this.next_image_job = 0;
}

SpiceDisplayConn.prototype = Object.create(SpiceConn.prototype);

SpiceDisplayConn.prototype.update_primary_monitor = function()
{
    var surface = this.surfaces && this.surfaces[this.primary_surface];
    if (!surface)
        return;
    var heads = (this.monitor_heads || []).filter(function(head) {
        return head.surface_id === surface.surface_id && head.id <= 255 &&
               head.width > 0 && head.height > 0 &&
               head.x >= 0 && head.y >= 0 &&
               head.x + head.width <= surface.width &&
               head.y + head.height <= surface.height;
    });
    var head = heads.find(function(candidate) { return candidate.id === 0; }) || heads[0];
    surface.canvas.spiceMonitor = head || null;
    this.parent.mousex = this.parent.mousey = undefined;
    if (this.parent.onmonitorchange)
        this.parent.onmonitorchange();
};

/*----------------------------------------------------------------------------
**  Draw queue
**      Every drawing message becomes an op on one in-order queue, and a
**      draw that decodes asynchronously (JPEG or worker image) holds the ops behind it
**      until it is ready, so what the server sent after the JPEG lands
**      after the JPEG. The queue is drained from a microtask at the end
**      of the task that filled it, which is the websocket message: no
**      added latency and no extra task per frame (a drain per animation
**      frame cost 0.5 ms of task time per MJPEG frame). A drain spends
**      at most FLUSH_BUDGET_MS and hands the rest to an animation frame
**      so input stays responsive under a burst; a timer stands in for
**      the frame in a hidden tab.
**--------------------------------------------------------------------------*/
var FLUSH_BUDGET_MS = 8;
var FLUSH_FALLBACK_MS = 100;
var MAX_DRAW_OPS = 1024;
var MAX_DRAW_AGE_MS = 3000;
var MAX_IMAGE_JOBS = 128;
var MAX_IMAGE_JOB_BYTES = 128 * 1024 * 1024;

SpiceDisplayConn.prototype.check_render_backlog = function()
{
    if (this.render_overloaded || !this.parent || !this.parent.onbacklog)
        return;
    var queued = this.ops.length - this.ops_head;
    var oldest = queued ? this.ops[this.ops_head] : undefined;
    if (queued <= MAX_DRAW_OPS &&
        (!oldest || performance.now() - oldest.queued_at <= MAX_DRAW_AGE_MS) &&
        this.image_jobs.size <= MAX_IMAGE_JOBS &&
        this.image_jobs_bytes <= MAX_IMAGE_JOB_BYTES)
        return;
    this.request_render_recovery();
}

SpiceDisplayConn.prototype.request_render_recovery = function()
{
    if (this.render_overloaded || !this.parent?.onbacklog)
        return false;
    this.render_overloaded = true;
    var parent = this.parent;
    /* Reconnecting inside a WebSocket message handler would tear down the
       channel while it is still parsing that message. */
    Promise.resolve().then(function() { parent.onbacklog(); });
    return true;
}

SpiceDisplayConn.prototype.watch_render_backlog = function()
{
    if (typeof window == 'undefined' || this.backlog_timer !== undefined ||
        this.render_overloaded || !this.parent?.onbacklog ||
        this.ops_head >= this.ops.length)
        return;
    var sc = this;
    var age = performance.now() - this.ops[this.ops_head].queued_at;
    this.backlog_timer = window.setTimeout(function()
    {
        sc.backlog_timer = undefined;
        sc.check_render_backlog();
        sc.watch_render_backlog();
    }, Math.max(1, MAX_DRAW_AGE_MS - age + 1));
}

SpiceDisplayConn.prototype.enqueue = function(draw)
{
    var op = { ready: true, draw: draw, queued_at: performance.now() };
    this.ops.push(op);
    this.check_render_backlog();
    this.watch_render_backlog();
    this.schedule_flush();
    return op;
}

/* An op whose draw is not known yet; mark_ready() supplies it. */
SpiceDisplayConn.prototype.enqueue_pending = function()
{
    var op = { ready: false, draw: null, queued_at: performance.now() };
    this.ops.push(op);
    this.check_render_backlog();
    this.watch_render_backlog();
    return op;
}

SpiceDisplayConn.prototype.mark_ready = function(op, draw)
{
    op.draw = draw;
    op.ready = true;
    this.schedule_flush();
}

SpiceDisplayConn.prototype.schedule_flush = function()
{
    if (this.flush_frame !== undefined || this.flush_micro)
        return;
    var sc = this;
    this.flush_micro = true;
    Promise.resolve().then(function() { sc.flush_micro = false; if (sc.flush_frame === undefined) sc.flush(FLUSH_BUDGET_MS); });
}

SpiceDisplayConn.prototype.schedule_frame = function()
{
    if (this.flush_frame !== undefined)
        return;
    var sc = this;
    this.flush_frame = window.requestAnimationFrame(function() { sc.flush(FLUSH_BUDGET_MS); });
    this.flush_timer = window.setTimeout(function() { sc.flush(FLUSH_BUDGET_MS); }, FLUSH_FALLBACK_MS);
}

SpiceDisplayConn.prototype.cancel_flush = function()
{
    if (this.flush_frame !== undefined)
    {
        window.cancelAnimationFrame(this.flush_frame);
        if (this.flush_timer !== undefined)
            window.clearTimeout(this.flush_timer);
        this.flush_frame = undefined;
        this.flush_timer = undefined;
    }
}

SpiceDisplayConn.prototype.flush = function(budget_ms)
{
    this.cancel_flush();
    var deadline = performance.now() + budget_ms;
    while (this.ops_head < this.ops.length && this.ops[this.ops_head].ready)
    {
        var op = this.ops[this.ops_head];
        this.ops[this.ops_head++] = undefined;
        if (op.draw)
            op.draw.call(this);
        if (performance.now() > deadline && this.ops_head < this.ops.length && this.ops[this.ops_head].ready)
        {
            this.compact_queue();
            this.schedule_frame();
            return;
        }
    }
    this.compact_queue();
    this.watch_render_backlog();
}

/* Avoid shifting every draw; compact consumed slots only occasionally. */
SpiceDisplayConn.prototype.compact_queue = function()
{
    if (this.ops_head === this.ops.length)
    {
        this.ops = [];
        this.ops_head = 0;
        if (this.backlog_timer !== undefined)
            window.clearTimeout(this.backlog_timer);
        this.backlog_timer = undefined;
    }
    else if (this.ops_head >= 64 && this.ops_head * 2 >= this.ops.length)
    {
        this.ops = this.ops.slice(this.ops_head);
        this.ops_head = 0;
    }
}

/* Drain everything that can be drawn now. */
SpiceDisplayConn.prototype.flush_all = function()
{
    this.flush(Infinity);
}

SpiceDisplayConn.prototype.drop_queue = function()
{
    this.cancel_flush();
    if (this.backlog_timer !== undefined)
        window.clearTimeout(this.backlog_timer);
    this.backlog_timer = undefined;
    this.ops = [];
    this.ops_head = 0;
}

SpiceDisplayConn.prototype.stop_image_worker = function()
{
    if (this.image_worker)
        this.image_worker.terminate();
    this.image_worker = undefined;
    this.image_jobs?.clear();
    this.image_jobs_bytes = 0;
}

/* Whether a surface captured when an op was queued is still the live
   one: a queued draw for a surface destroyed and recreated since is moot. */
SpiceDisplayConn.prototype.surface_live = function(surface)
{
    return surface !== undefined && this.surfaces !== undefined &&
           this.surfaces[surface.surface_id] === surface;
}

SpiceDisplayConn.prototype.cleanup = function()
{
    this.stop_image_worker();
    this.drop_queue();
    SpiceConn.prototype.cleanup.call(this);
}
SpiceDisplayConn.prototype.process_channel_message = function(msg)
{
    /* A reconnect has already been requested. Avoid decoding and retaining
       more display messages from the current WebSocket task. */
    if (this.render_overloaded)
        return true;
    if (msg.type == Constants.SPICE_MSG_DISPLAY_MODE)
    {
        this.known_unimplemented(msg.type, "Display Mode");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_MARK)
    {
        // FIXME - DISPLAY_MARK not implemented (may be hard or impossible)
        this.known_unimplemented(msg.type, "Display Mark");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_RESET)
    {
        Utils.DEBUG > 2 && console.log("Display reset");
        var reset_surface = this.surfaces[this.primary_surface];
        this.enqueue(function()
        {
            if (this.surface_live(reset_surface))
                reset_surface.canvas.context.restore();
        });
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_DRAW_COPY)
    {
        var draw_copy = new Messages.SpiceMsgDisplayDrawCopy(msg.data);

        Utils.DEBUG > 1 && this.log_draw("DrawCopy", draw_copy);

        if (draw_copy.data.rop_descriptor != Constants.SPICE_ROPD_OP_PUT)
            this.log_warn("FIXME: DrawCopy we don't handle ropd type: " + draw_copy.data.rop_descriptor);
        if (draw_copy.data.mask.flags)
            this.log_warn("FIXME: DrawCopy we don't handle mask flag: " + draw_copy.data.mask.flags);
        if (draw_copy.data.mask.bitmap)
            this.log_warn("FIXME: DrawCopy we don't handle mask");

        if (draw_copy.data && draw_copy.data.src_bitmap)
        {
            if (draw_copy.data.src_bitmap.descriptor.flags &&
                draw_copy.data.src_bitmap.descriptor.flags != Constants.SPICE_IMAGE_FLAGS_CACHE_ME &&
                draw_copy.data.src_bitmap.descriptor.flags != Constants.SPICE_IMAGE_FLAGS_HIGH_BITS_SET)
            {
                this.log_warn("FIXME: DrawCopy unhandled image flags: " + draw_copy.data.src_bitmap.descriptor.flags);
                Utils.DEBUG <= 1 && this.log_draw("DrawCopy", draw_copy);
            }

            if (draw_copy.data.src_bitmap.descriptor.type == Constants.SPICE_IMAGE_TYPE_QUIC)
            {
                if (! draw_copy.data.src_bitmap.quic)
                {
                    this.log_warn("FIXME: DrawCopy could not handle this QUIC file.");
                    return false;
                }
                return this.draw_copy_async(
                    { base: draw_copy.base,
                      src_area: draw_copy.data.src_area,
                      tag: "copyquic",
                      descriptor : draw_copy.data.src_bitmap.descriptor,
                      scale_mode : draw_copy.data.scale_mode
                    }, 'quic', draw_copy.data.src_bitmap.quic);
            }
            else if (draw_copy.data.src_bitmap.descriptor.type == Constants.SPICE_IMAGE_TYPE_FROM_CACHE ||
                    draw_copy.data.src_bitmap.descriptor.type == Constants.SPICE_IMAGE_TYPE_FROM_CACHE_LOSSLESS)
            {
                /* Cache writes and invalidations run in draw order, so look
                   up the image when this operation reaches the queue head. */
                var cache_id = draw_copy.data.src_bitmap.descriptor.id;
                var sc = this;
                return this.draw_copy_helper(
                    { base: draw_copy.base,
                      src_area: draw_copy.data.src_area,
                      resolve: function()
                      {
                          if (sc.cache && sc.cache[cache_id])
                              return sc.cache[cache_id];
                          sc.log_warn("FIXME: DrawCopy did not find image id " + cache_id + " in cache.");
                          return undefined;
                      },
                      tag: "copycache." + cache_id,
                      has_alpha: true, /* FIXME - may want this to be false... */
                      descriptor : draw_copy.data.src_bitmap.descriptor,
                      scale_mode : draw_copy.data.scale_mode
                    });

                /* FIXME - LOSSLESS CACHE ramifications not understood or handled */
            }
            else if (draw_copy.data.src_bitmap.descriptor.type == Constants.SPICE_IMAGE_TYPE_SURFACE)
            {
                var source_surface = this.surfaces[draw_copy.data.src_bitmap.surface_id];
                var src_area = draw_copy.data.src_area;
                var computed_src_area = new SpiceRect;
                computed_src_area.top = computed_src_area.left = 0;
                computed_src_area.right = src_area.right - src_area.left;
                computed_src_area.bottom = src_area.bottom - src_area.top;
                var sc = this;

                /* FIXME - there is a potential optimization here.
                           That is, if the surface is from 0,0, and
                           both surfaces are alpha surfaces, you should
                           be able to just do a drawImage, which should
                           save time.  */

                /* The source is read when this op runs, after everything
                   queued for it has been drawn. */
                return this.draw_copy_helper(
                    { base: draw_copy.base,
                      src_area: computed_src_area,
                      resolve: function()
                      {
                          if (! sc.surface_live(source_surface))
                              return undefined;
                          return source_surface.canvas.context.getImageData(
                              src_area.left, src_area.top,
                              computed_src_area.right, computed_src_area.bottom);
                      },
                      tag: "copysurf." + draw_copy.data.src_bitmap.surface_id,
                      has_alpha: source_surface.format != Constants.SPICE_SURFACE_FMT_32_xRGB,
                      descriptor : draw_copy.data.src_bitmap.descriptor,
                      scale_mode : draw_copy.data.scale_mode
                    });
            }
            else if (draw_copy.data.src_bitmap.descriptor.type == Constants.SPICE_IMAGE_TYPE_JPEG)
            {
                if (! draw_copy.data.src_bitmap.jpeg)
                {
                    this.log_warn("FIXME: DrawCopy could not handle this JPEG file.");
                    return false;
                }

                var img = new Image;
                img.o =
                    { base: draw_copy.base,
                      tag: "jpeg." + draw_copy.data.src_bitmap.surface_id,
                      descriptor : draw_copy.data.src_bitmap.descriptor,
                      sc : this,
                      surface : this.surfaces[draw_copy.base.surface_id],
                      op : this.enqueue_pending(),
                      src_area : draw_copy.data.src_area,
                      scale_mode : draw_copy.data.scale_mode,
                    };
                img.onload = handle_draw_jpeg_onload;
                img.onerror = handle_draw_jpeg_onerror;
                img.src = jpeg_image_url(draw_copy.data.src_bitmap.jpeg.data);

                return true;
            }
            else if (draw_copy.data.src_bitmap.descriptor.type == Constants.SPICE_IMAGE_TYPE_JPEG_ALPHA)
            {
                if (! draw_copy.data.src_bitmap.jpeg_alpha)
                {
                    this.log_warn("FIXME: DrawCopy could not handle this JPEG ALPHA file.");
                    return false;
                }

                var img = new Image;
                img.o =
                    { base: draw_copy.base,
                      tag: "jpeg." + draw_copy.data.src_bitmap.surface_id,
                      descriptor : draw_copy.data.src_bitmap.descriptor,
                      sc : this,
                      surface : this.surfaces[draw_copy.base.surface_id],
                      op : this.enqueue_pending(),
                      src_area : draw_copy.data.src_area,
                      scale_mode : draw_copy.data.scale_mode,
                    };

                if (this.surfaces[draw_copy.base.surface_id].format == Constants.SPICE_SURFACE_FMT_32_ARGB)
                {
                    img.o.alpha_pending = true;
                    var sc = this;
                    var alpha = draw_copy.data.src_bitmap.jpeg_alpha.alpha;
                    this.queue_image_job({ kind: 'lz', source: alpha,
                        o: { surface: this.surfaces[draw_copy.base.surface_id] },
                        width: alpha.width, height: alpha.height,
                        finish: function(image)
                        {
                            img.alpha_img = image;
                            img.o.alpha_failed = !image;
                            img.o.alpha_ready = true;
                            if (img.o.jpeg_ready && !img.o.finished)
                            {
                                img.o.finished = true;
                                sc.mark_ready(img.o.op,
                                    image ? function() { draw_jpeg_now(img); } : null);
                            }
                        } });
                }
                img.onload = handle_draw_jpeg_onload;
                img.onerror = handle_draw_jpeg_onerror;
                img.src = jpeg_image_url(draw_copy.data.src_bitmap.jpeg_alpha.data);

                return true;
            }
            else if (draw_copy.data.src_bitmap.descriptor.type == Constants.SPICE_IMAGE_TYPE_BITMAP)
            {
                if (! draw_copy.data.src_bitmap.bitmap)
                {
                    this.log_err("null bitmap");
                    return false;
                }

                return this.draw_copy_async(
                    { base: draw_copy.base,
                      src_area: draw_copy.data.src_area,
                      tag: "bitmap." + draw_copy.data.src_bitmap.bitmap.format,
                      has_alpha: draw_copy.data.src_bitmap.bitmap.format != Constants.SPICE_BITMAP_FMT_32BIT,
                      descriptor : draw_copy.data.src_bitmap.descriptor,
                      scale_mode : draw_copy.data.scale_mode
                    }, 'bitmap', draw_copy.data.src_bitmap.bitmap);
            }
            else if (draw_copy.data.src_bitmap.descriptor.type == Constants.SPICE_IMAGE_TYPE_LZ_RGB)
            {
                if (! draw_copy.data.src_bitmap.lz_rgb)
                {
                    this.log_err("null lz_rgb ");
                    return false;
                }

                return this.draw_copy_async(
                    { base: draw_copy.base,
                      src_area: draw_copy.data.src_area,
                      tag: "lz_rgb." + draw_copy.data.src_bitmap.lz_rgb.type,
                      has_alpha: draw_copy.data.src_bitmap.lz_rgb.type == Constants.LZ_IMAGE_TYPE_RGBA ? true : false ,
                      descriptor : draw_copy.data.src_bitmap.descriptor,
                      scale_mode : draw_copy.data.scale_mode
                    }, 'lz', draw_copy.data.src_bitmap.lz_rgb);
            }
            else
            {
                this.log_warn("FIXME: DrawCopy unhandled image type: " + draw_copy.data.src_bitmap.descriptor.type);
                this.log_draw("DrawCopy", draw_copy);
                return false;
            }
        }

        this.log_warn("FIXME: DrawCopy no src_bitmap.");
        return false;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_DRAW_FILL)
    {
        var draw_fill = new Messages.SpiceMsgDisplayDrawFill(msg.data);

        Utils.DEBUG > 1 && this.log_draw("DrawFill", draw_fill);

        if (draw_fill.data.rop_descriptor != Constants.SPICE_ROPD_OP_PUT)
            this.log_warn("FIXME: DrawFill we don't handle ropd type: " + draw_fill.data.rop_descriptor);
        if (draw_fill.data.mask.flags)
            this.log_warn("FIXME: DrawFill we don't handle mask flag: " + draw_fill.data.mask.flags);
        if (draw_fill.data.mask.bitmap)
            this.log_warn("FIXME: DrawFill we don't handle mask");

        if (draw_fill.data.brush.type == Constants.SPICE_BRUSH_TYPE_SOLID)
        {
            // FIXME - do brushes ever have alpha?
            var color = draw_fill.data.brush.color & 0xffffff;
            var color_str = "rgb(" + (color >> 16) + ", " + ((color >> 8) & 0xff) + ", " + (color & 0xff) + ")";
            var fill_surface = this.surfaces[draw_fill.base.surface_id];

            this.enqueue(function()
            {
                if (! this.surface_live(fill_surface))
                    return;
                var fill_context = fill_surface.canvas.context;
                fill_context.fillStyle = color_str;

                with_clip(fill_context, draw_fill.base.clip, function()
                {
                    fill_context.fillRect(
                        draw_fill.base.box.left, draw_fill.base.box.top,
                        draw_fill.base.box.right - draw_fill.base.box.left,
                        draw_fill.base.box.bottom - draw_fill.base.box.top);
                });

                if (Utils.DUMP_DRAWS && this.parent.dump_id)
                {
                    var debug_canvas = document.createElement("canvas");
                    debug_canvas.setAttribute('width', fill_surface.canvas.width);
                    debug_canvas.setAttribute('height', fill_surface.canvas.height);
                    debug_canvas.setAttribute('id', "fillbrush." + draw_fill.base.surface_id + "." + fill_surface.draw_count);
                    debug_canvas.getContext("2d").fillStyle = color_str;
                    debug_canvas.getContext("2d").fillRect(
                        draw_fill.base.box.left, draw_fill.base.box.top,
                        draw_fill.base.box.right - draw_fill.base.box.left,
                        draw_fill.base.box.bottom - draw_fill.base.box.top);
                    document.getElementById(this.parent.dump_id).appendChild(debug_canvas);
                }

                fill_surface.draw_count++;
            });

        }
        else
        {
            this.log_warn("FIXME: DrawFill can't handle brush type: " + draw_fill.data.brush.type);
        }
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_DRAW_OPAQUE)
    {
        this.known_unimplemented(msg.type, "Display Draw Opaque");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_DRAW_BLEND)
    {
        this.known_unimplemented(msg.type, "Display Draw Blend");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_DRAW_BLACKNESS)
    {
        this.known_unimplemented(msg.type, "Display Draw Blackness");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_DRAW_WHITENESS)
    {
        this.known_unimplemented(msg.type, "Display Draw Whiteness");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_DRAW_INVERS)
    {
        this.known_unimplemented(msg.type, "Display Draw Invers");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_DRAW_ROP3)
    {
        this.known_unimplemented(msg.type, "Display Draw ROP3");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_DRAW_STROKE)
    {
        this.known_unimplemented(msg.type, "Display Draw Stroke");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_DRAW_TRANSPARENT)
    {
        this.known_unimplemented(msg.type, "Display Draw Transparent");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_DRAW_ALPHA_BLEND)
    {
        this.known_unimplemented(msg.type, "Display Draw Alpha Blend");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_COPY_BITS)
    {
        var copy_bits = new Messages.SpiceMsgDisplayCopyBits(msg.data);

        Utils.DEBUG > 1 && this.log_draw("CopyBits", copy_bits);

        var copy_surface = this.surfaces[copy_bits.base.surface_id];

        this.enqueue(function()
        {
            if (! this.surface_live(copy_surface))
                return;
            var source_canvas = copy_surface.canvas;
            var source_context = source_canvas.context;

            var width = source_canvas.width - copy_bits.src_pos.x;
            var height = source_canvas.height - copy_bits.src_pos.y;
            if (width > (copy_bits.base.box.right - copy_bits.base.box.left))
                width = copy_bits.base.box.right - copy_bits.base.box.left;
            if (height > (copy_bits.base.box.bottom - copy_bits.base.box.top))
                height = copy_bits.base.box.bottom - copy_bits.base.box.top;

            /* drawImage from a canvas onto itself snapshots the source rect
               first (per the 2D canvas spec), so this replaces a getImageData
               round-trip — a full GPU->CPU sync readback per scroll — with a
               blit that stays on the GPU. */
            with_clip(source_context, copy_bits.base.clip, function()
            {
                source_context.drawImage(source_canvas,
                        copy_bits.src_pos.x, copy_bits.src_pos.y, width, height,
                        copy_bits.base.box.left, copy_bits.base.box.top, width, height);
            });

            if (Utils.DUMP_DRAWS && this.parent.dump_id)
            {
                var debug_canvas = document.createElement("canvas");
                debug_canvas.setAttribute('width', width);
                debug_canvas.setAttribute('height', height);
                debug_canvas.setAttribute('id', "copybits" + copy_bits.base.surface_id + "." + copy_surface.draw_count);
                debug_canvas.getContext("2d").drawImage(source_canvas,
                    copy_bits.base.box.left, copy_bits.base.box.top, width, height,
                    0, 0, width, height);
                document.getElementById(this.parent.dump_id).appendChild(debug_canvas);
            }

            copy_surface.draw_count++;
        });
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_INVAL_ALL_PIXMAPS)
    {
        this.enqueue(function() { this.cache = {}; });
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_INVAL_PALETTE)
    {
        this.known_unimplemented(msg.type, "Display Inval Palette");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_INVAL_ALL_PALETTES)
    {
        this.known_unimplemented(msg.type, "Inval All Palettes");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_SURFACE_CREATE)
    {
        if (! ("surfaces" in this))
            this.surfaces = [];

        var m = new Messages.SpiceMsgSurfaceCreate(msg.data);
        Utils.DEBUG > 1 && console.log(this.type + ": MsgSurfaceCreate id " + m.surface.surface_id
                                    + "; " + m.surface.width + "x" + m.surface.height
                                    + "; format " + m.surface.format
                                    + "; flags " + m.surface.flags);
        if (m.surface.format != Constants.SPICE_SURFACE_FMT_32_xRGB &&
            m.surface.format != Constants.SPICE_SURFACE_FMT_32_ARGB)
        {
            this.log_warn("FIXME: cannot handle surface format " + m.surface.format + " yet.");
            return false;
        }

        /* Resolution changes can reuse the primary surface ID before its
           queued destroy runs. Do not leave the old canvas first in the
           screen: fitDisplay would size and map the mouse against it. */
        if (this.surfaces[m.surface.surface_id] !== undefined)
            this.delete_surface(m.surface.surface_id);

        var canvas = document.createElement("canvas");
        canvas.setAttribute('width', m.surface.width);
        canvas.setAttribute('height', m.surface.height);
        canvas.setAttribute('id', "spice_surface_" + m.surface.surface_id);
        canvas.setAttribute('tabindex', m.surface.surface_id);
        canvas.context = canvas.getContext("2d");

        /* A fresh canvas is fully transparent; a real SPICE client presents a
           black framebuffer. Without this, regions the guest never draws
           (e.g. during firmware boot) show the page background through. */
        canvas.context.fillStyle = "black";
        canvas.context.fillRect(0, 0, m.surface.width, m.surface.height);

        if (Utils.DUMP_CANVASES && this.parent.dump_id)
            document.getElementById(this.parent.dump_id).appendChild(canvas);

        m.surface.canvas = canvas;
        m.surface.draw_count = 0;
        this.surfaces[m.surface.surface_id] = m.surface;

        if (m.surface.flags & Constants.SPICE_SURFACE_FLAGS_PRIMARY)
        {
            this.primary_surface = m.surface.surface_id;
            this.parent.mousex = this.parent.mousey = undefined;

            /* This .save() is done entirely to enable SPICE_MSG_DISPLAY_RESET */
            canvas.context.save();
            document.getElementById(this.parent.screen_id).appendChild(canvas);

            /* We're going to leave width dynamic, but correctly set the height */
            document.getElementById(this.parent.screen_id).style.height = m.surface.height + "px";
            this.hook_events();
            this.update_primary_monitor();
        }
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_SURFACE_DESTROY)
    {
        var m = new Messages.SpiceMsgSurfaceDestroy(msg.data);
        Utils.DEBUG > 1 && console.log(this.type + ": MsgSurfaceDestroy id " + m.surface_id);
        var doomed = this.surfaces ? this.surfaces[m.surface_id] : undefined;
        if (doomed === undefined)
            return true;
        /* Stop hit testing and sizing against this canvas immediately. Draws
           queued before the destroy may still use its detached pixels. */
        this.retire_surface(doomed);
        this.enqueue(function()
        {
            if (this.surface_live(doomed))
                this.delete_surface(m.surface_id);
        });
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_STREAM_CREATE)
    {
        var m = new Messages.SpiceMsgDisplayStreamCreate(msg.data);
        Utils.STREAM_DEBUG > 0 && console.log(this.type + ": MsgStreamCreate id" + m.id + "; type " + m.codec_type +
                                        "; width " + m.stream_width + "; height " + m.stream_height +
                                        "; left " + m.dest.left + "; top " + m.dest.top
                                        );
        if (!this.streams)
            this.streams = new Array();
        if (this.streams[m.id])
            console.log("Stream " + m.id + " already exists");
        else
            this.streams[m.id] = m;

        if (m.codec_type == Constants.SPICE_VIDEO_CODEC_TYPE_VP8)
        {
            var media = new MediaSource();
            var v = document.createElement("video");
            v.src = window.URL.createObjectURL(media);

            v.setAttribute('muted', true);
            v.setAttribute('autoplay', true);
            v.setAttribute('width', m.stream_width);
            v.setAttribute('height', m.stream_height);

            var left = m.dest.left;
            var top = m.dest.top;
            if (this.surfaces[m.surface_id] !== undefined)
            {
                left += this.surfaces[m.surface_id].canvas.offsetLeft;
                top += this.surfaces[m.surface_id].canvas.offsetTop;
            }
            document.getElementById(this.parent.screen_id).appendChild(v);
            v.setAttribute('style', "pointer-events:none; position: absolute; top:" + top + "px; left:" + left + "px;");
            if (! (m.flags & Constants.SPICE_STREAM_FLAGS_TOP_DOWN))
                v.style.transform = "scaleY(-1)";

            media.addEventListener('sourceopen', handle_video_source_open, false);
            media.addEventListener('sourceended', handle_video_source_ended, false);
            media.addEventListener('sourceclosed', handle_video_source_closed, false);

            var s = this.streams[m.id];
            s.video = v;
            s.media = media;
            s.queue = new Array();
            s.start_time = null;
            s.cluster_time = 0;
            s.append_okay = false;
            s.waiting_for_keyframe = false;

            media.stream = s;
            media.spiceconn = this;
            v.spice_stream = s;
            apply_video_clip(s);
        }
        else if (m.codec_type == Constants.SPICE_VIDEO_CODEC_TYPE_MJPEG)
            this.streams[m.id].frames_loading = 0;
        else
            console.log("Unhandled stream codec: "+m.codec_type);
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_STREAM_DATA ||
        msg.type == Constants.SPICE_MSG_DISPLAY_STREAM_DATA_SIZED)
    {
        var m;
        if (msg.type == Constants.SPICE_MSG_DISPLAY_STREAM_DATA_SIZED)
            m = new Messages.SpiceMsgDisplayStreamDataSized(msg.data);
        else
            m = new Messages.SpiceMsgDisplayStreamData(msg.data);

        if (!this.streams || !this.streams[m.base.id])
        {
            console.log("no stream for data");
            return false;
        }

        var time_until_due = m.base.multi_media_time - this.parent.relative_now();

        if (this.streams[m.base.id].codec_type === Constants.SPICE_VIDEO_CODEC_TYPE_MJPEG)
            process_mjpeg_stream_data(this, m, time_until_due);

        if (this.streams[m.base.id].codec_type === Constants.SPICE_VIDEO_CODEC_TYPE_VP8)
            process_video_stream_data(this.streams[m.base.id], m);

        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_STREAM_ACTIVATE_REPORT)
    {
        var m = new Messages.SpiceMsgDisplayStreamActivateReport(msg.data);

        var report = new Messages.SpiceMsgcDisplayStreamReport(m.stream_id, m.unique_id);
        if (this.streams && this.streams[m.stream_id])
        {
            this.streams[m.stream_id].report = report;
            this.streams[m.stream_id].max_window_size = m.max_window_size;
            this.streams[m.stream_id].timeout_ms = m.timeout_ms
        }

        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_STREAM_CLIP)
    {
        var m = new Messages.SpiceMsgDisplayStreamClip(msg.data);
        Utils.STREAM_DEBUG > 1 && console.log(this.type + ": MsgStreamClip id" + m.id);
        /* A clip for a stream that was already destroyed must not throw:
           an exception in a handler desyncs the channel framing. */
        if (this.streams && this.streams[m.id])
        {
            this.streams[m.id].clip = m.clip;
            apply_video_clip(this.streams[m.id]);
        }
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_STREAM_DESTROY)
    {
        var m = new Messages.SpiceMsgDisplayStreamDestroy(msg.data);
        Utils.STREAM_DEBUG > 0 && console.log(this.type + ": MsgStreamDestroy id" + m.id);

        /* A destroy for an unknown or already-destroyed id must not throw:
           an exception here skips the wire reader's rearm and desyncs the
           channel framing for good. */
        if (this.streams && this.streams[m.id])
            this.destroy_stream(m.id);
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_STREAM_DESTROY_ALL)
    {
        this.known_unimplemented(msg.type, "Display Stream Destroy All");
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_INVAL_LIST)
    {
        var m = new Messages.SpiceMsgDisplayInvalList(msg.data);
        Utils.DEBUG > 1 && console.log(this.type + ": MsgInvalList " + m.count + " items");
        this.enqueue(function()
        {
            for (var i = 0; i < m.count; i++)
                if (this.cache)
                    delete this.cache[m.resources[i].id];
        });
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_MONITORS_CONFIG)
    {
        var config = new Messages.SpiceMsgDisplayMonitorsConfig(msg.data);
        if (!config.heads)
        {
            this.log_warn("Ignoring malformed display monitors config");
            return true;
        }
        this.monitor_heads = config.heads;
        this.update_primary_monitor();
        return true;
    }

    if (msg.type == Constants.SPICE_MSG_DISPLAY_DRAW_COMPOSITE)
    {
        this.known_unimplemented(msg.type, "Display Draw Composite");
        return true;
    }

    return false;
}

SpiceDisplayConn.prototype.retire_surface = function(surface)
{
    if (this.primary_surface == surface.surface_id &&
        this.surfaces[surface.surface_id] === surface)
    {
        this.unhook_events();
        this.primary_surface = undefined;
        this.parent.mousex = this.parent.mousey = undefined;
    }
    if (surface.canvas.parentNode)
        surface.canvas.parentNode.removeChild(surface.canvas);
}

SpiceDisplayConn.prototype.delete_surface = function(surface_id)
{
    var surface = this.surfaces && this.surfaces[surface_id];
    if (surface === undefined)
        return;
    this.retire_surface(surface);
    delete this.surfaces[surface_id];
}


SpiceDisplayConn.prototype.prepare_draw_copy = function(o)
{
    o.surface = this.surfaces[o.base.surface_id];

    /* FIXME - This is based on trial + error, not a serious thoughtful
               analysis of what Spice requires.  See display.js for more. */
    o.opaque = ! o.has_alpha || o.surface.format == Constants.SPICE_SURFACE_FMT_32_xRGB;
}

SpiceDisplayConn.prototype.cache_draw_copy = function(o)
{
    /* Cache changes follow protocol draw order, including invalidations. */
    if (o.image_data && o.descriptor && (o.descriptor.flags & Constants.SPICE_IMAGE_FLAGS_CACHE_ME))
    {
        if (o.opaque && o.has_alpha)
            stripAlpha(o.image_data);
        if (! ("cache" in this))
            this.cache = {};
        this.cache[o.descriptor.id] = o.image_data;
    }
}

SpiceDisplayConn.prototype.draw_copy_helper = function(o)
{
    this.prepare_draw_copy(o);

    this.enqueue(function()
    {
        this.cache_draw_copy(o);
        this.draw_copy_now(o);
    });
    return true;
}

SpiceDisplayConn.prototype.decode_image_now = function(kind, source, context)
{
    if (kind == 'quic')
        return Quic.convert_spice_quic_to_web(context, source);
    if (kind == 'lz')
        return convert_spice_lz_to_web(context, source);
    return convert_spice_bitmap_to_web(context, source);
}

SpiceDisplayConn.prototype.complete_image_job = function(job, image_data, type)
{
    if (job.kind == 'quic')
        job.o.has_alpha = type == Quic.Constants.QUIC_IMAGE_TYPE_RGBA;
    job.o.opaque = !job.o.has_alpha || job.o.surface.format == Constants.SPICE_SURFACE_FMT_32_xRGB;
    job.o.image_data = image_data;
    if (!image_data)
        this.log_warn('Could not decode SPICE ' + job.kind + ' image');
    var o = job.o;
    this.mark_ready(job.op, image_data ? function()
    {
        this.cache_draw_copy(o);
        this.draw_copy_now(o);
    } : null);
}

SpiceDisplayConn.prototype.finish_image_job = function(job, image_data, type)
{
    if (job.finish)
        job.finish(image_data, type);
    else
        this.complete_image_job(job, image_data, type);
}

SpiceDisplayConn.prototype.fail_image_worker = function()
{
    if (this.image_worker_disabled)
        return;
    this.image_worker_disabled = true;
    var jobs = Array.from(this.image_jobs.values());
    var bytes = this.image_jobs_bytes;
    this.stop_image_worker();
    /* A failed worker should not move an entire decode backlog onto the
       input thread. A fresh connection may start a new worker. */
    if ((jobs.length > 8 || bytes > 16 * 1024 * 1024) &&
        this.request_render_recovery())
        return;
    for (var i = 0; i < jobs.length; i++)
    {
        var job = jobs[i];
        try
        {
            var image = this.decode_image_now(job.kind, job.source, job.o.surface.canvas.context);
            this.finish_image_job(job, image, job.source.type);
        }
        catch (error)
        {
            this.log_warn('SPICE image decode failed: ' + error);
            this.finish_image_job(job, undefined);
        }
    }
}

SpiceDisplayConn.prototype.ensure_image_worker = function()
{
    if (this.image_worker_disabled || typeof Worker == 'undefined')
        return false;
    if (this.image_worker)
        return true;
    try
    {
        var sc = this;
        this.image_worker = new Worker(new URL('./image-worker.js', import.meta.url), { type: 'module' });
        this.image_worker.onmessage = function(event)
        {
            var result = event.data;
            var job = sc.image_jobs.get(result.id);
            if (!job)
                return;
            sc.image_jobs.delete(result.id);
            sc.image_jobs_bytes -= job.bytes;
            if (result.error)
            {
                sc.log_warn('SPICE image worker: ' + result.error);
                sc.finish_image_job(job, undefined);
                return;
            }
            try
            {
                var pixels = new Uint8ClampedArray(result.pixels);
                var image;
                if (typeof ImageData != 'undefined')
                    image = new ImageData(pixels, result.width, result.height);
                else
                {
                    image = job.o.surface.canvas.context.createImageData(result.width, result.height);
                    image.data.set(pixels);
                }
                sc.finish_image_job(job, image, result.type);
            }
            catch (error)
            {
                sc.log_warn('SPICE image result failed: ' + error);
                sc.finish_image_job(job, undefined);
            }
        };
        this.image_worker.onerror = function(event)
        {
            event.preventDefault();
            sc.fail_image_worker();
        };
        this.image_worker.onmessageerror = function() { sc.fail_image_worker(); };
        return true;
    }
    catch (error)
    {
        this.image_worker_disabled = true;
        return false;
    }
}

SpiceDisplayConn.prototype.queue_image_job = function(job)
{
    var source = job.source;
    var kind = job.kind;
    if (this.ensure_image_worker())
    {
        var width = Math.max(job.width, source.width || source.x || 0);
        var height = Math.max(job.height, source.height || source.y || 0);
        job.bytes = (source.data?.byteLength || 0) + width * height * 4;
        var id = ++this.next_image_job;
        this.image_jobs.set(id, job);
        this.image_jobs_bytes += job.bytes;
        this.check_render_backlog();
        if (this.render_overloaded)
            return true;
        try
        {
            this.image_worker.postMessage({ id: id, kind: kind, source: source });
            return true;
        }
        catch (error)
        {
            this.fail_image_worker();
            return true;
        }
    }
    try
    {
        var image = this.decode_image_now(kind, source, job.o.surface.canvas.context);
        this.finish_image_job(job, image, source.type);
    }
    catch (error)
    {
        this.log_warn('SPICE image decode failed: ' + error);
        this.finish_image_job(job, undefined);
    }
    return true;
}

SpiceDisplayConn.prototype.draw_copy_async = function(o, kind, source)
{
    this.prepare_draw_copy(o);
    return this.queue_image_job({ o: o, kind: kind, source: source,
        width: o.descriptor.width, height: o.descriptor.height,
        op: this.enqueue_pending() });
}

SpiceDisplayConn.prototype.draw_copy_now = function(o)
{
    if (! this.surface_live(o.surface))
        return;
    var image_data = o.image_data || (o.resolve ? o.resolve() : undefined);
    if (! image_data)
        return;

    var canvas = o.surface.canvas;
    var left = o.base.box.left;
    var top = o.base.box.top;
    var width = o.base.box.right - left;
    var height = o.base.box.bottom - top;
    var src = source_rect(o, image_data.width, image_data.height);
    var scaled = (src.right - src.left) != width || (src.bottom - src.top) != height;
    if (o.opaque && o.has_alpha)
        stripAlpha(image_data);

    /* src_area picks the part of the image that lands in the box, and a
       box of another size scales it. putImageData can offset but not
       scale, so a scaled draw goes through drawImage, which needs the
       alpha bytes made opaque above to copy rather than blend. */
    if (scaled)
    {
        canvas.context.imageSmoothingEnabled = o.scale_mode != Constants.SPICE_IMAGE_SCALE_MODE_NEAREST;
        with_clip(canvas.context, o.base.clip, function()
        {
            putImageDataWithAlpha(canvas.context, image_data, left, top, src, width, height);
        });
        canvas.context.imageSmoothingEnabled = true;
    }
    else if (is_clipped(o.base.clip))
    {
        if (o.opaque)
            putImageDataClipped(canvas.context, image_data, left, top, o.base.clip, src);
        else
            with_clip(canvas.context, o.base.clip, function()
            {
                putImageDataWithAlpha(canvas.context, image_data, left, top, src, width, height);
            });
    }
    else if (o.opaque)
        canvas.context.putImageData(image_data, left - src.left, top - src.top, src.left, src.top, width, height);
    else
        putImageDataWithAlpha(canvas.context, image_data, left, top, src, width, height);

    if (Utils.DUMP_DRAWS && this.parent.dump_id)
    {
        var debug_canvas = document.createElement("canvas");
        debug_canvas.setAttribute('width', image_data.width);
        debug_canvas.setAttribute('height', image_data.height);
        debug_canvas.setAttribute('id', o.tag + "." +
            o.surface.draw_count + "." +
            o.base.surface_id + "@" + o.base.box.left + "x" +  o.base.box.top);
        debug_canvas.getContext("2d").putImageData(image_data, 0, 0);
        document.getElementById(this.parent.dump_id).appendChild(debug_canvas);
    }

    o.surface.draw_count++;
}


SpiceDisplayConn.prototype.log_draw = function(prefix, draw)
{
    var str = prefix + "." + draw.base.surface_id + "." + this.surfaces[draw.base.surface_id].draw_count + ": ";
    str += "base.box " + draw.base.box.left + ", " + draw.base.box.top + " to " +
                           draw.base.box.right + ", " + draw.base.box.bottom;
    str += "; clip.type " + draw.base.clip.type;

    if (draw.data)
    {
        if (draw.data.src_area)
            str += "; src_area " + draw.data.src_area.left + ", " + draw.data.src_area.top + " to "
                                 + draw.data.src_area.right + ", " + draw.data.src_area.bottom;

        if (draw.data.src_bitmap && draw.data.src_bitmap != null)
        {
            str += "; src_bitmap id: " + draw.data.src_bitmap.descriptor.id;
            str += "; src_bitmap width " + draw.data.src_bitmap.descriptor.width + ", height " + draw.data.src_bitmap.descriptor.height;
            str += "; src_bitmap type " + draw.data.src_bitmap.descriptor.type + ", flags " + draw.data.src_bitmap.descriptor.flags;
            if (draw.data.src_bitmap.surface_id !== undefined)
                str += "; src_bitmap surface_id " + draw.data.src_bitmap.surface_id;
            if (draw.data.src_bitmap.bitmap)
                str += "; BITMAP format " + draw.data.src_bitmap.bitmap.format +
                        "; flags " + draw.data.src_bitmap.bitmap.flags +
                        "; x " + draw.data.src_bitmap.bitmap.x +
                        "; y " + draw.data.src_bitmap.bitmap.y +
                        "; stride " + draw.data.src_bitmap.bitmap.stride ;
            if (draw.data.src_bitmap.quic)
                str += "; QUIC type " + draw.data.src_bitmap.quic.type +
                        "; width " + draw.data.src_bitmap.quic.width +
                        "; height " + draw.data.src_bitmap.quic.height ;
            if (draw.data.src_bitmap.lz_rgb)
                str += "; LZ_RGB length " + draw.data.src_bitmap.lz_rgb.length +
                       "; magic " + draw.data.src_bitmap.lz_rgb.magic +
                       "; version 0x" + draw.data.src_bitmap.lz_rgb.version.toString(16) +
                       "; type " + draw.data.src_bitmap.lz_rgb.type +
                       "; width " + draw.data.src_bitmap.lz_rgb.width +
                       "; height " + draw.data.src_bitmap.lz_rgb.height +
                       "; stride " + draw.data.src_bitmap.lz_rgb.stride +
                       "; top down " + draw.data.src_bitmap.lz_rgb.top_down;
        }
        else
            str += "; src_bitmap is null";

        if (draw.data.brush)
        {
            if (draw.data.brush.type == Constants.SPICE_BRUSH_TYPE_SOLID)
                str += "; brush.color 0x" + draw.data.brush.color.toString(16);
            if (draw.data.brush.type == Constants.SPICE_BRUSH_TYPE_PATTERN)
            {
                str += "; brush.pat ";
                if (draw.data.brush.pattern.pat != null)
                    str += "[SpiceImage]";
                else
                    str += "[null]";
                str += " at " + draw.data.brush.pattern.pos.x + ", " + draw.data.brush.pattern.pos.y;
            }
        }

        str += "; rop_descriptor " + draw.data.rop_descriptor;
        if (draw.data.scale_mode !== undefined)
            str += "; scale_mode " + draw.data.scale_mode;
        str += "; mask.flags " + draw.data.mask.flags;
        str += "; mask.pos " + draw.data.mask.pos.x + ", " + draw.data.mask.pos.y;
        if (draw.data.mask.bitmap != null)
        {
            str += "; mask.bitmap width " + draw.data.mask.bitmap.descriptor.width + ", height " + draw.data.mask.bitmap.descriptor.height;
            str += "; mask.bitmap type " + draw.data.mask.bitmap.descriptor.type + ", flags " + draw.data.mask.bitmap.descriptor.flags;
        }
        else
            str += "; mask.bitmap is null";
    }

    console.log(str);
}

SpiceDisplayConn.prototype.hook_events = function()
{
    if (this.primary_surface !== undefined)
    {
        var canvas = this.surfaces[this.primary_surface].canvas;
        canvas.sc = this.parent;
        canvas.addEventListener('mousemove', Inputs.handle_mousemove);
        canvas.addEventListener('mousedown', Inputs.handle_mousedown);
        canvas.addEventListener('contextmenu', Inputs.handle_contextmenu);
        canvas.addEventListener('mouseup', Inputs.handle_mouseup);
        canvas.addEventListener('keydown', Inputs.handle_keydown);
        canvas.addEventListener('keyup', Inputs.handle_keyup);
        canvas.addEventListener('mouseout', handle_mouseout);
        canvas.addEventListener('mouseover', handle_mouseover);
        canvas.addEventListener('wheel', Inputs.handle_mousewheel);
        canvas.focus();

        this.focusListener = () => this.parent.send_clipboard_grab()
        // send host clipboard when the canvas is rendered initially
        this.focusListener();
        // register focus event to grab host clipboard when the canvas gets focus
        canvas.addEventListener('focus', this.focusListener);
    }
}

SpiceDisplayConn.prototype.unhook_events = function()
{
    if (this.primary_surface !== undefined)
    {
        var canvas = this.surfaces[this.primary_surface].canvas;
        canvas.removeEventListener('mousemove', Inputs.handle_mousemove);
        canvas.removeEventListener('mousedown', Inputs.handle_mousedown);
        canvas.removeEventListener('contextmenu', Inputs.handle_contextmenu);
        canvas.removeEventListener('mouseup', Inputs.handle_mouseup);
        canvas.removeEventListener('keydown', Inputs.handle_keydown);
        canvas.removeEventListener('keyup', Inputs.handle_keyup);
        canvas.removeEventListener('mouseout', handle_mouseout);
        canvas.removeEventListener('mouseover', handle_mouseover);
        canvas.removeEventListener('wheel', Inputs.handle_mousewheel);
        canvas.removeEventListener('focus', this.focusListener);
    }
}


SpiceDisplayConn.prototype.destroy_stream = function(id)
{
    var stream = this.streams[id];
    if (stream.codec_type == Constants.SPICE_VIDEO_CODEC_TYPE_VP8)
    {
        if (stream.video)
        {
            if (stream.video.parentNode)
                stream.video.parentNode.removeChild(stream.video);
            /* The blob URL registration outlives the element; without the
               revoke each stream create/destroy cycle leaked one. */
            window.URL.revokeObjectURL(stream.video.src);
        }
        stream.source_buffer = null;
        stream.media = null;
        stream.video = null;
        stream.queue = [];
    }
    this.streams[id] = undefined;
}

SpiceDisplayConn.prototype.destroy_surfaces = function()
{
    this.drop_queue();
    for (var s in this.surfaces)
    {
        this.delete_surface(this.surfaces[s].surface_id);
    }

    this.surfaces = undefined;

    /* Streams own DOM video elements and MediaSources that live in the
       screen div, not in a surface; a client-side stop mid-stream left
       them behind to pile up across reconnects. */
    if (this.streams)
    {
        for (var i = 0; i < this.streams.length; i++)
        {
            if (this.streams[i])
                this.destroy_stream(i);
        }
        this.streams = undefined;
    }
}


function handle_mouseover(e)
{
    this.focus();
}

function handle_mouseout(e)
{
    if (this.sc && this.sc.cursor && this.sc.cursor.spice_simulated_cursor)
        this.sc.cursor.spice_simulated_cursor.style.display = 'none';
    this.blur();
}

function handle_draw_jpeg_onload()
{
    /* The decoded bitmap survives the revoke; without it every frame's
       blob stays registered for the life of the page. */
    revoke_jpeg_image_url(this);

    if (this.o.sc.streams && this.o.sc.streams[this.o.id])
        this.o.sc.streams[this.o.id].frames_loading--;

    this.o.jpeg_ready = true;
    if ((!this.o.alpha_pending || this.o.alpha_ready) && !this.o.finished)
    {
        this.o.finished = true;
        var img = this;
        this.o.sc.mark_ready(this.o.op,
            this.o.alpha_failed ? null : function() { draw_jpeg_now(img); });
    }
}

/* Runs from the draw queue once every op queued before the JPEG has run. */
function draw_jpeg_now(img)
{
    var sc = img.o.sc;
    var o = img.o;

    /*------------------------------------------------------------
    ** FIXME:
    **  The helper should be extended to be able to handle actual HtmlImageElements
    **  ...and the cache should be modified to do so as well
    **----------------------------------------------------------*/
    if (! sc.surface_live(o.surface))
    {
        // The surface was destroyed (e.g. open a menu, close it quickly)
        //  or the connection stopped while the image was decoding.
        Utils.DEBUG > 2 && sc.log_info("Discarding jpeg; presumed lost surface " + o.base.surface_id);
        img.onload = undefined;
        img.src = Utils.EMPTY_GIF_IMAGE;
        return;
    }
    var context = o.surface.canvas.context;
    var left = o.base.box.left;
    var top = o.base.box.top;
    var width = o.base.box.right - left;
    var height = o.base.box.bottom - top;
    var src = source_rect(o, img.width, img.height);
    var sw = src.right - src.left;
    var sh = src.bottom - src.top;
    context.imageSmoothingEnabled = o.scale_mode != Constants.SPICE_IMAGE_SCALE_MODE_NEAREST;

    if (img.alpha_img)
    {
        var c = document.createElement("canvas");
        var t = c.getContext("2d");
        c.setAttribute('width', img.alpha_img.width);
        c.setAttribute('height', img.alpha_img.height);
        t.putImageData(img.alpha_img, 0, 0);
        t.globalCompositeOperation = 'source-in';
        t.drawImage(img, 0, 0);

        with_clip(context, o.base.clip, function()
        {
            context.drawImage(c, src.left, src.top, sw, sh, left, top, width, height);
        });

        if (o.descriptor &&
            (o.descriptor.flags & Constants.SPICE_IMAGE_FLAGS_CACHE_ME))
        {
            if (! ("cache" in sc))
                sc.cache = {};

            sc.cache[o.descriptor.id] =
                t.getImageData(0, 0,
                    img.alpha_img.width,
                    img.alpha_img.height);
        }
    }
    else
    {
        with_clip(context, o.base.clip, function()
        {
            if (o.bottom_up)
            {
                /* The encoder emits a bottom-up frame's rows in memory
                   order, last row first; flip it back while blitting. */
                context.save();
                context.translate(left, top + height);
                context.scale(1, -1);
                context.drawImage(img, src.left, src.top, sw, sh, 0, 0, width, height);
                context.restore();
            }
            else
                context.drawImage(img, src.left, src.top, sw, sh, left, top, width, height);
        });

        if (o.descriptor &&
            (o.descriptor.flags & Constants.SPICE_IMAGE_FLAGS_CACHE_ME))
        {
            if (! ("cache" in sc))
                sc.cache = {};

            /* The cache wants the whole image; a clipped, offset or scaled
               draw left only part of it, or a resampling, on the surface. */
            var whole = src.left == 0 && src.top == 0 && sw == img.width && sh == img.height &&
                        width == img.width && height == img.height;
            sc.cache[o.descriptor.id] = (is_clipped(o.base.clip) || ! whole) ?
                image_to_image_data(img, img.width, img.height) :
                context.getImageData(left, top, width, height);
        }

        // Give the Garbage collector a clue to recycle this; avoids
        //  fairly massive memory leaks during video playback
        img.onload = undefined;
        img.src = Utils.EMPTY_GIF_IMAGE;
    }

    context.imageSmoothingEnabled = true;

    if (Utils.DUMP_DRAWS && sc.parent.dump_id)
    {
        var debug_canvas = document.createElement("canvas");
        debug_canvas.setAttribute('id', o.tag + "." +
            o.surface.draw_count + "." +
            o.base.surface_id + "@" + o.base.box.left + "x" +  o.base.box.top);
        debug_canvas.getContext("2d").drawImage(img, 0, 0);
        document.getElementById(sc.parent.dump_id).appendChild(debug_canvas);
    }

    o.surface.draw_count++;

    if (sc.streams && sc.streams[o.id] && "report" in sc.streams[o.id])
        process_stream_data_report(sc, o.id, o.msg_mmtime, o.msg_mmtime - sc.parent.relative_now());
}

function process_mjpeg_stream_data(sc, m, time_until_due)
{
    /* If we are currently processing an mjpeg frame when a new one arrives,
       and the new one is 'late', drop the new frame.  This helps the browsers
       keep up, and provides rate control feedback as well */
    if (time_until_due < 0 && sc.streams[m.base.id].frames_loading > 0)
    {
        if ("report" in sc.streams[m.base.id])
            sc.streams[m.base.id].report.num_drops++;
        return;
    }

    var img = new Image;
    var strm_base = new Messages.SpiceMsgDisplayBase();
    strm_base.surface_id = sc.streams[m.base.id].surface_id;
    strm_base.box = m.dest || sc.streams[m.base.id].dest;
    strm_base.clip = sc.streams[m.base.id].clip;
    img.o =
        { base: strm_base,
          tag: "mjpeg." + m.base.id,
          descriptor: null,
          sc : sc,
          id : m.base.id,
          msg_mmtime : m.base.multi_media_time,
          bottom_up : ! (sc.streams[m.base.id].flags & Constants.SPICE_STREAM_FLAGS_TOP_DOWN),
          surface : sc.surfaces ? sc.surfaces[strm_base.surface_id] : undefined,
          op : sc.enqueue_pending(),
        };
    img.onload = handle_draw_jpeg_onload;
    img.onerror = handle_draw_jpeg_onerror;
    img.src = jpeg_image_url(m.data);

    sc.streams[m.base.id].frames_loading++;
}

function process_stream_data_report(sc, id, msg_mmtime, time_until_due)
{
    sc.streams[id].report.num_frames++;
    if (sc.streams[id].report.start_frame_mm_time == 0)
        sc.streams[id].report.start_frame_mm_time = msg_mmtime;

    if (sc.streams[id].report.num_frames > sc.streams[id].max_window_size ||
        (msg_mmtime - sc.streams[id].report.start_frame_mm_time) > sc.streams[id].timeout_ms)
    {
        sc.streams[id].report.end_frame_mm_time = msg_mmtime;
        sc.streams[id].report.last_frame_delay = time_until_due;

        var msg = new Messages.SpiceMiniData();
        msg.build_msg(Constants.SPICE_MSGC_DISPLAY_STREAM_REPORT, sc.streams[id].report);
        sc.send_msg(msg);

        sc.streams[id].report.start_frame_mm_time = 0;
        sc.streams[id].report.num_frames = 0;
        sc.streams[id].report.num_drops = 0;
    }
}

function handle_video_source_open(e)
{
    var stream = this.stream;
    var p = this.spiceconn;

    if (stream.source_buffer)
        return;

    var s = this.addSourceBuffer(Webm.Constants.SPICE_VP8_CODEC);
    if (! s)
    {
        p.log_err('Codec ' + Webm.Constants.SPICE_VP8_CODEC + ' not available.');
        return;
    }

    stream.source_buffer = s;
    s.spiceconn = p;
    s.stream = stream;

    listen_for_video_events(stream);

    var h = new Webm.Header();
    var te = new Webm.VideoTrackEntry(this.stream.stream_width, this.stream.stream_height);
    var t = new Webm.Tracks(te);

    var mb = new ArrayBuffer(h.buffer_size() + t.buffer_size())

    var b = h.to_buffer(mb);
    t.to_buffer(mb, b);

    s.addEventListener('error', handle_video_buffer_error, false);
    s.addEventListener('updateend', handle_append_video_buffer_done, false);

    append_video_buffer(s, mb);
}

function handle_video_source_ended(e)
{
    var p = this.spiceconn;
    p.log_err('Video source unexpectedly ended.');
}

function handle_video_source_closed(e)
{
    var p = this.spiceconn;
    p.log_err('Video source unexpectedly closed.');
}

function append_video_buffer(sb, mb)
{
    try
    {
        sb.stream.append_okay = false;
        sb.appendBuffer(mb);
    }
    catch (e)
    {
        var p = sb.spiceconn;
        p.log_err("Error invoking appendBuffer: " + e.message);
    }
}

function handle_append_video_buffer_done(e)
{
    var stream = this.stream;
    if (!stream.video)
        return;

    if (stream.current_frame && stream.current_frame.kind == "frame" && "report" in stream)
    {
        var sc = stream.media.spiceconn;
        var t = stream.current_frame.msg_mmtime;
        process_stream_data_report(sc, stream.id, t, t - sc.parent.relative_now());
    }

    if (stream.queue.length > 0)
    {
        stream.current_frame = stream.queue.shift();
        append_video_buffer(stream.source_buffer, stream.current_frame.mb);
    }
    else
    {
        stream.current_frame = null;
        stream.append_okay = true;
    }

    if (stream.video.buffered.length > 0)
    {
        var last = stream.video.buffered.length - 1;
        var start = stream.video.buffered.start(last);
        var end = stream.video.buffered.end(last);
        /* MediaSource can retain a continuous old range even after our JS
           queue is trimmed. Seek near the live edge when playback is late. */
        if (stream.video.currentTime < start || end - stream.video.currentTime > 2)
            stream.video.currentTime = Math.max(start, end - 0.3);
    }

    /* Modern browsers try not to auto play video. */
    if (this.stream.video.paused && this.stream.video.readyState >= 2)
        this.stream.video.play();

    if (Utils.STREAM_DEBUG > 1)
        console.log(stream.video.currentTime + ":id " +  stream.id + " updateend " + Utils.dump_media_element(stream.video));
}

function handle_video_buffer_error(e)
{
    var p = this.spiceconn;
    p.log_err('source_buffer error ' + e.message);
}

var MAX_VP8_QUEUE_FRAMES = 30;
var MAX_VP8_QUEUE_BYTES = 8 * 1024 * 1024;
var MAX_VP8_QUEUE_MS = 1000;

function vp8_is_keyframe(data)
{
    /* The low bit of the VP8 uncompressed frame tag is zero for a keyframe.
       WebM cluster boundaries alone say nothing about decoder references. */
    return data.byteLength > 0 && (data[0] & 1) == 0;
}

function report_video_drop(stream, time)
{
    if ("report" in stream && stream.media && stream.media.spiceconn)
    {
        var sc = stream.media.spiceconn;
        stream.report.num_drops++;
        process_stream_data_report(sc, stream.id, time, time - sc.parent.relative_now());
    }
}

function push_or_queue(stream, msg, mb, kind, keyframe)
{
    var frame =
    {
        msg_mmtime : msg.base.multi_media_time,
        kind : kind,
        keyframe : !!keyframe,
        data : kind == "frame" ? msg.data : null,
    };

    if (stream.append_okay)
    {
        stream.current_frame = frame;
        append_video_buffer(stream.source_buffer, mb);
    }
    else
    {
        frame.mb = mb;
        stream.queue.push(frame);
    }
}

function video_simple_block(stream, msg, keyframe)
{
    var simple = new Webm.SimpleBlock(msg.base.multi_media_time - stream.cluster_time, msg.data, keyframe);
    var mb = new ArrayBuffer(simple.buffer_size());
    simple.to_buffer(mb);

    push_or_queue(stream, msg, mb, "frame", keyframe);
}

function new_video_cluster(stream, msg, keyframe)
{
    stream.cluster_time = msg.base.multi_media_time;
    var c = new Webm.Cluster(stream.cluster_time - stream.start_time, msg.data);

    var mb = new ArrayBuffer(c.buffer_size());
    c.to_buffer(mb);

    push_or_queue(stream, msg, mb, "cluster", false);

    video_simple_block(stream, msg, keyframe);
}

function trim_video_queue(stream)
{
    var frames = stream.queue.filter(function(item) { return item.kind == "frame"; });
    if (frames.length == 0)
        return;
    var bytes = stream.queue.reduce(function(total, item) { return total + item.mb.byteLength; }, 0);
    var age = frames[frames.length - 1].msg_mmtime - frames[0].msg_mmtime;
    if (frames.length <= MAX_VP8_QUEUE_FRAMES && bytes <= MAX_VP8_QUEUE_BYTES && age <= MAX_VP8_QUEUE_MS)
        return;

    /* Keep the newest decodable run: its keyframe and every later delta.
       Dropping a delta inside that run could break all following frames.
       Re-encode the WebM timecodes against a fresh cluster. */
    var latest = frames[frames.length - 1].msg_mmtime;
    var keyframe_index = -1;
    for (var i = frames.length - 1; i >= 0; i--)
    {
        if (frames[i].keyframe)
        {
            keyframe_index = i;
            break;
        }
    }
    var kept = keyframe_index < 0 ? [] : frames.slice(keyframe_index);
    var kept_bytes = kept.reduce(function(total, item) { return total + item.mb.byteLength; }, 0);
    if (kept.length > MAX_VP8_QUEUE_FRAMES || kept_bytes > MAX_VP8_QUEUE_BYTES ||
        (kept.length && latest - kept[0].msg_mmtime > MAX_VP8_QUEUE_MS))
        kept = [];

    stream.queue = [];
    if (kept.length)
    {
        /* The source buffer is still busy, so these become the new queue. */
        for (var j = 0; j < kept.length; j++)
        {
            var msg = { base: { multi_media_time: kept[j].msg_mmtime }, data: kept[j].data };
            if (j == 0)
                new_video_cluster(stream, msg, true);
            else
                video_simple_block(stream, msg, false);
        }
    }
    else
        stream.waiting_for_keyframe = true;

    for (var k = 0; k < frames.length - kept.length; k++)
    {
        report_video_drop(stream, frames[k].msg_mmtime);
    }
}

function process_video_stream_data(stream, msg)
{
    var keyframe = vp8_is_keyframe(msg.data);
    if (stream.waiting_for_keyframe)
    {
        if (!keyframe)
        {
            report_video_drop(stream, msg.base.multi_media_time);
            return;
        }
        stream.waiting_for_keyframe = false;
        if (stream.start_time === null)
            stream.start_time = msg.base.multi_media_time;
        new_video_cluster(stream, msg, true);
    }
    else if (stream.start_time === null)
    {
        if (!keyframe)
        {
            stream.waiting_for_keyframe = true;
            report_video_drop(stream, msg.base.multi_media_time);
            return;
        }
        stream.start_time = msg.base.multi_media_time;
        new_video_cluster(stream, msg, true);
    }

    else if (msg.base.multi_media_time - stream.cluster_time >= Webm.Constants.MAX_CLUSTER_TIME)
        new_video_cluster(stream, msg, keyframe);
    else
        video_simple_block(stream, msg, keyframe);

    trim_video_queue(stream);
}

function video_handle_event_debug(e)
{
    var s = this.spice_stream;
    if (s.video)
    {
        if (Utils.STREAM_DEBUG > 0 || s.video.buffered.len > 1)
            console.log(s.video.currentTime + ":id " +  s.id + " event " + e.type +
                Utils.dump_media_element(s.video));
    }

    if (Utils.STREAM_DEBUG > 1 && s.media)
        console.log("  media_source " + Utils.dump_media_source(s.media));

    if (Utils.STREAM_DEBUG > 1 && s.source_buffer)
        console.log("  source_buffer " + Utils.dump_source_buffer(s.source_buffer));

    if (Utils.STREAM_DEBUG > 1 || s.queue.length > 1)
        console.log('  queue len ' + s.queue.length + '; append_okay: ' + s.append_okay);
}

function video_debug_listen_for_one_event(name)
{
    this.addEventListener(name, video_handle_event_debug);
}

function listen_for_video_events(stream)
{
    var video_0_events = [
        "abort", "error"
    ];

    var video_1_events = [
        "loadstart", "suspend", "emptied", "stalled", "loadedmetadata", "loadeddata", "canplay",
        "canplaythrough", "playing", "waiting", "seeking", "seeked", "ended", "durationchange",
        "play", "pause", "ratechange"
    ];

    var video_2_events = [
        "timeupdate",
        "progress",
        "resize",
        "volumechange"
    ];

    video_0_events.forEach(video_debug_listen_for_one_event, stream.video);
    if (Utils.STREAM_DEBUG > 0)
        video_1_events.forEach(video_debug_listen_for_one_event, stream.video);
    if (Utils.STREAM_DEBUG > 1)
        video_2_events.forEach(video_debug_listen_for_one_event, stream.video);
}

export {
  SpiceDisplayConn,
  process_video_stream_data,
  handle_append_video_buffer_done,
};
