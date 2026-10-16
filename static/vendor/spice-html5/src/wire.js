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

/*--------------------------------------------------------------------------------------
**  SpiceWireReader
**      This class will receive messages from a WebSocket and relay it to a given
**  callback.  It will optionally save and pass along a header, useful in processing
**  the mini message format.
**--------------------------------------------------------------------------------------*/

import { DEBUG } from './utils.js';

function SpiceWireReader(sc, callback)
{
    this.sc = sc;
    this.callback = callback;
    this.needed = 0;
    this.size = 0;

    this.buffers = [];
    this.buffer_head = 0;
    this.buffer_offset = 0;

    this.sc.ws.wire_reader = this;
    this.sc.ws.binaryType = "arraybuffer";
    this.sc.ws.addEventListener('message', wire_blob_catcher);
}

SpiceWireReader.prototype =
{

    /*------------------------------------------------------------------------
    **  Process messages coming in from our WebSocket
    **----------------------------------------------------------------------*/
    inbound: function (mb)
    {
        if (mb.byteLength)
        {
            this.buffers.push(mb);
            this.size += mb.byteLength;
        }

        /* A WebSocket packet may contain several SPICE messages, or only
           part of one. Keep unread bytes in the original packet and copy
           only when a message crosses packet boundaries. The callback can
           change needed after each header or body. */
        while (this.needed > 0 && this.size >= this.needed)
        {
            var needed = this.needed;
            var first = this.buffers[this.buffer_head];
            var available = first.byteLength - this.buffer_offset;
            var frame;
            if (available >= needed)
            {
                frame = this.buffer_offset === 0 && available === needed ?
                    first : first.slice(this.buffer_offset, this.buffer_offset + needed);
                this.buffer_offset += needed;
                if (this.buffer_offset === first.byteLength)
                {
                    this.buffer_head++;
                    this.buffer_offset = 0;
                }
            }
            else
            {
                frame = new ArrayBuffer(needed);
                var view = new Uint8Array(frame);
                var copied = 0;
                while (copied < needed)
                {
                    var buffer = this.buffers[this.buffer_head];
                    var take = Math.min(buffer.byteLength - this.buffer_offset, needed - copied);
                    view.set(new Uint8Array(buffer, this.buffer_offset, take), copied);
                    copied += take;
                    this.buffer_offset += take;
                    if (this.buffer_offset === buffer.byteLength)
                    {
                        this.buffer_head++;
                        this.buffer_offset = 0;
                    }
                }
            }
            this.size -= needed;
            if (this.buffer_head === this.buffers.length)
            {
                this.buffers = [];
                this.buffer_head = 0;
            }
            else if (this.buffer_head >= 64 && this.buffer_head * 2 >= this.buffers.length)
            {
                this.buffers = this.buffers.slice(this.buffer_head);
                this.buffer_head = 0;
            }
            this.callback.call(this.sc, frame, this.saved_msg_header || undefined);
        }
    },

    request: function(n)
    {
        this.needed = n;
    },

    save_header: function(h)
    {
        this.saved_msg_header = h;
    },

    clear_header: function()
    {
        this.saved_msg_header = undefined;
    },
}

function wire_blob_catcher(e)
{
    this.wire_reader.sc.networkObserver?.record?.('received', e.data.byteLength);
    DEBUG > 1 && console.log(">> WebSockets.onmessage");
    DEBUG > 1 && console.log("id " + this.wire_reader.sc.connection_id +"; type " + this.wire_reader.sc.type);
    SpiceWireReader.prototype.inbound.call(this.wire_reader, e.data);
}

export {
  SpiceWireReader,
};
