/* SPDX-License-Identifier: BSD-2-Clause */

#include <string.h>

#include "m2sdr_tool.h"

int m2sdr_tool_parse_dma_header(const uint8_t *buf, uint64_t *timestamp)
{
    uint64_t sync_word = 0;
    uint64_t ts = 0;

    if (!buf || !timestamp)
        return 0;

    memcpy(&sync_word, buf, sizeof(sync_word));
    /* An RX gain-tagged header (0x5aa5 sync, version 1 at bits [19:16]; see m2sdr.h) keeps the
     * timestamp word, so it parses like the legacy sync word. */
    if (sync_word != M2SDR_TOOL_DMA_HEADER_SYNC_WORD &&
        !((sync_word & 0xffffu) == 0x5aa5u && ((sync_word >> 16) & 0xfu) == 1u))
        return 0;

    memcpy(&ts, buf + 8, sizeof(ts));
    *timestamp = ts;
    return 1;
}

void m2sdr_tool_write_dma_header(uint8_t *buf, uint64_t timestamp)
{
    const uint64_t sync_word = M2SDR_TOOL_DMA_HEADER_SYNC_WORD;

    if (!buf)
        return;

    memcpy(buf, &sync_word, sizeof(sync_word));
    memcpy(buf + 8, &timestamp, sizeof(timestamp));
}
