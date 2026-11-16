/* SPDX-License-Identifier: BSD-2-Clause
 *
 * LiteX-M2SDR RX gain calibration tables (dBFS -> dBm).
 *
 * Reads the per-board RX gain tables the bladeRF tooling produces, in either form:
 *
 * - CSV: "Serial: <serial>", the RX header line (GAIN_CAL_HEADER_RX below), then one row per
 *   measurement: RX chain, commanded RX gain (dB), signal generator power into the RX port (dBm),
 *   signal frequency (Hz), tuned LO (Hz), AD9361 RSSI register, measured power (dBFS).
 * - .tbl: the same rows in a libbladeRF image container ("bladeRF" magic, SHA-256, version,
 *   timestamp, serial, 128 reserved bytes, then big-endian type/address/length and the data:
 *   the 33-byte serial followed by packed little-endian 33-byte rows).
 *
 * Each row gives the conversion the receive chain achieved at that LO and commanded gain G:
 *
 *     gain_corr(f) = (dBFS - dBm_in) - G
 *
 * which for the bladeRF tables (all swept at G = 0) is exactly libbladeRF's gain_corr. A capture
 * at nominal gain G' (the AD9361 table value for the gain index in force) then obeys
 *
 *     dBFS = dBm_in + G' + gain_corr(f)      =>      dBm_in = dBFS - (G' + gain_corr(f)).
 *
 * gain_corr between rows is interpolated linearly in LO frequency, and clamped to the first/last
 * row outside the table, as libbladeRF does.
 */

#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "m2sdr.h"
#include "m2sdr_internal.h"

#define GAIN_CAL_HEADER_RX "RX Chain,RX Gain,VSG Power into bladeRF RX (dBm),Frequency of signal (Hz),Frequency of bladeRF+PXI (Hz),AD9361 RSSI register value,Power of Signal from Full Scale (dBFS)"

#define TBL_MAGIC            "bladeRF"
#define TBL_MAGIC_LEN        7u
#define TBL_CHECKSUM_LEN     32u
#define TBL_SERIAL_LEN       33u
#define TBL_RESERVED_LEN     128u
#define TBL_HEADER_LEN       (TBL_MAGIC_LEN + TBL_CHECKSUM_LEN + 3u * 2u + 8u + TBL_SERIAL_LEN + \
                              TBL_RESERVED_LEN + 3u * 4u)
#define TBL_RX_ROW_LEN       (1u + 4u + 4u + 8u + 8u + 4u + 4u)

struct m2sdr_gain_cal_row {
    uint8_t  chain;
    int32_t  gain;
    float    vsg_dbm;
    uint64_t lo_hz;
    float    dbfs;
};

struct m2sdr_gain_cal {
    unsigned channel;
    char serial[M2SDR_GAIN_CAL_SERIAL_MAX];
    char path[M2SDR_GAIN_CAL_PATH_MAX];
    int32_t gain_db;                 /* commanded gain of the rows used */
    size_t n_entries;
    struct m2sdr_gain_cal_entry *entries; /* sorted by frequency */
};

/* Helpers -----------------------------------------------------------------------------------------*/

static int cmp_entry(const void *a, const void *b)
{
    const struct m2sdr_gain_cal_entry *x = a, *y = b;
    return (x->freq_hz > y->freq_hz) - (x->freq_hz < y->freq_hz);
}

/* Keep the rows of one channel and one commanded gain, then sort. bladeRF numbers RX chains as
 * libbladeRF channels (RX1 = 0, RX2 = 2); a table written with plain indices (RX2 = 1) is accepted
 * when the channel numbering finds nothing, as libbladeRF does. */
static int build_table(struct m2sdr_gain_cal *cal, const struct m2sdr_gain_cal_row *rows,
                       size_t n_rows)
{
    for (int pass = 0; pass < 2; pass++) {
        uint8_t chain = (uint8_t)(pass == 0 ? 2u * cal->channel : cal->channel);
        size_t n = 0;
        int have_gain = 0;
        int32_t gain = 0;

        if (pass == 1 && chain == 2u * cal->channel)
            break; /* RX1: both numberings are 0. */
        for (size_t i = 0; i < n_rows; i++) {
            if (rows[i].chain != chain)
                continue;
            if (!have_gain) {
                gain = rows[i].gain;
                have_gain = 1;
            }
            if (rows[i].gain == gain)
                n++;
        }
        if (n == 0)
            continue;

        cal->entries = calloc(n, sizeof(*cal->entries));
        if (!cal->entries)
            return M2SDR_ERR_NO_MEM;
        cal->gain_db = gain;
        for (size_t i = 0; i < n_rows; i++) {
            if (rows[i].chain != chain || rows[i].gain != gain)
                continue;
            struct m2sdr_gain_cal_entry *e = &cal->entries[cal->n_entries++];
            e->freq_hz     = rows[i].lo_hz;
            e->gain_corr_db = (double)rows[i].dbfs - (double)rows[i].vsg_dbm - (double)gain;
        }
        qsort(cal->entries, cal->n_entries, sizeof(*cal->entries), cmp_entry);
        return M2SDR_ERR_OK;
    }
    return M2SDR_ERR_RANGE; /* No rows for this channel. */
}

static int load_csv(FILE *f, struct m2sdr_gain_cal_row **rows_out, size_t *n_out, char *serial)
{
    char line[1024];
    size_t cap = 0, n = 0;
    struct m2sdr_gain_cal_row *rows = NULL;

    if (!fgets(line, sizeof(line), f) || sscanf(line, "Serial: %32s", serial) != 1)
        return M2SDR_ERR_PARSE;
    if (!fgets(line, sizeof(line), f) || strncmp(line, GAIN_CAL_HEADER_RX, strlen(GAIN_CAL_HEADER_RX)) != 0)
        return M2SDR_ERR_PARSE; /* A TX table, or not a gain table. */

    while (fgets(line, sizeof(line), f)) {
        unsigned chain;
        int32_t gain, rssi;
        float vsg, dbfs;
        uint64_t sig_hz, lo_hz;

        if (line[0] == '\n' || line[0] == '\r' || line[0] == '\0')
            continue;
        if (sscanf(line, "%u,%" SCNi32 ",%f,%" SCNu64 ",%" SCNu64 ",%" SCNi32 ",%f",
                   &chain, &gain, &vsg, &sig_hz, &lo_hz, &rssi, &dbfs) != 7) {
            free(rows);
            return M2SDR_ERR_PARSE;
        }
        if (n == cap) {
            size_t ncap = cap ? cap * 2 : 1024;
            struct m2sdr_gain_cal_row *grown = realloc(rows, ncap * sizeof(*rows));
            if (!grown) {
                free(rows);
                return M2SDR_ERR_NO_MEM;
            }
            rows = grown;
            cap = ncap;
        }
        rows[n].chain   = (uint8_t)chain;
        rows[n].gain    = gain;
        rows[n].vsg_dbm = vsg;
        rows[n].lo_hz   = lo_hz;
        rows[n].dbfs    = dbfs;
        n++;
    }
    *rows_out = rows;
    *n_out = n;
    return M2SDR_ERR_OK;
}

static uint32_t be32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

static int load_tbl(const uint8_t *buf, size_t len, struct m2sdr_gain_cal_row **rows_out,
                    size_t *n_out, char *serial)
{
    size_t data_len, n;
    const uint8_t *data;
    struct m2sdr_gain_cal_row *rows;

    if (len < TBL_HEADER_LEN || memcmp(buf, TBL_MAGIC, TBL_MAGIC_LEN) != 0)
        return M2SDR_ERR_PARSE;
    data_len = be32(buf + TBL_HEADER_LEN - 4u);
    if (len != TBL_HEADER_LEN + data_len || data_len < TBL_SERIAL_LEN ||
        (data_len - TBL_SERIAL_LEN) % TBL_RX_ROW_LEN != 0)
        return M2SDR_ERR_PARSE; /* Truncated, or TX rows (a different row size). */

    data = buf + TBL_HEADER_LEN;
    memcpy(serial, data, TBL_SERIAL_LEN - 1);
    serial[TBL_SERIAL_LEN - 1] = '\0';
    n = (data_len - TBL_SERIAL_LEN) / TBL_RX_ROW_LEN;
    rows = calloc(n ? n : 1, sizeof(*rows));
    if (!rows)
        return M2SDR_ERR_NO_MEM;

    const uint8_t *p = data + TBL_SERIAL_LEN;
    for (size_t i = 0; i < n; i++, p += TBL_RX_ROW_LEN) {
        /* Packed little-endian, field order of libbladeRF's gain_cal_csv_to_bin(). */
        int32_t rssi;
        uint64_t sig_hz;
        memcpy(&rows[i].chain,   p + 0,  1);
        memcpy(&rows[i].gain,    p + 1,  4);
        memcpy(&rows[i].vsg_dbm, p + 5,  4);
        memcpy(&sig_hz,          p + 9,  8);
        memcpy(&rows[i].lo_hz,   p + 17, 8);
        memcpy(&rssi,            p + 25, 4);
        memcpy(&rows[i].dbfs,    p + 29, 4);
        (void)rssi;
        (void)sig_hz;
    }
    *rows_out = rows;
    *n_out = n;
    return M2SDR_ERR_OK;
}

/* Standalone tables -------------------------------------------------------------------------------*/

int m2sdr_gain_cal_load(const char *path, unsigned channel, struct m2sdr_gain_cal **cal_out)
{
    FILE *f;
    uint8_t magic[TBL_MAGIC_LEN];
    struct m2sdr_gain_cal_row *rows = NULL;
    size_t n_rows = 0;
    struct m2sdr_gain_cal *cal;
    int rc;

    if (!path || !cal_out || channel > 1)
        return M2SDR_ERR_INVAL;
    *cal_out = NULL;

    cal = calloc(1, sizeof(*cal));
    if (!cal)
        return M2SDR_ERR_NO_MEM;
    cal->channel = channel;
    snprintf(cal->path, sizeof(cal->path), "%s", path);

    f = fopen(path, "rb");
    if (!f) {
        free(cal);
        return M2SDR_ERR_IO;
    }
    if (fread(magic, 1, sizeof(magic), f) == sizeof(magic) &&
        memcmp(magic, TBL_MAGIC, TBL_MAGIC_LEN) == 0) {
        long len;
        uint8_t *buf = NULL;
        if (fseek(f, 0, SEEK_END) != 0 || (len = ftell(f)) < 0 || fseek(f, 0, SEEK_SET) != 0) {
            rc = M2SDR_ERR_IO;
        } else if (!(buf = malloc((size_t)len ? (size_t)len : 1))) {
            rc = M2SDR_ERR_NO_MEM;
        } else if (fread(buf, 1, (size_t)len, f) != (size_t)len) {
            rc = M2SDR_ERR_IO;
        } else {
            rc = load_tbl(buf, (size_t)len, &rows, &n_rows, cal->serial);
        }
        free(buf);
    } else {
        rewind(f);
        rc = load_csv(f, &rows, &n_rows, cal->serial);
    }
    fclose(f);

    if (rc == M2SDR_ERR_OK)
        rc = build_table(cal, rows, n_rows);
    free(rows);
    if (rc != M2SDR_ERR_OK) {
        m2sdr_gain_cal_free(cal);
        return rc;
    }
    *cal_out = cal;
    return M2SDR_ERR_OK;
}

void m2sdr_gain_cal_free(struct m2sdr_gain_cal *cal)
{
    if (!cal)
        return;
    free(cal->entries);
    free(cal);
}

int m2sdr_gain_cal_get_info(const struct m2sdr_gain_cal *cal, struct m2sdr_gain_cal_info *info)
{
    if (!cal || !info)
        return M2SDR_ERR_INVAL;
    memset(info, 0, sizeof(*info));
    info->channel       = cal->channel;
    info->n_entries     = (uint32_t)cal->n_entries;
    info->gain_db       = cal->gain_db;
    info->start_freq_hz = cal->n_entries ? cal->entries[0].freq_hz : 0;
    info->stop_freq_hz  = cal->n_entries ? cal->entries[cal->n_entries - 1].freq_hz : 0;
    snprintf(info->serial, sizeof(info->serial), "%s", cal->serial);
    snprintf(info->path, sizeof(info->path), "%s", cal->path);
    return M2SDR_ERR_OK;
}

int m2sdr_gain_cal_get_entries(const struct m2sdr_gain_cal *cal, const struct m2sdr_gain_cal_entry **entries,
                               size_t *n_entries)
{
    if (!cal || !entries || !n_entries)
        return M2SDR_ERR_INVAL;
    *entries = cal->entries;
    *n_entries = cal->n_entries;
    return M2SDR_ERR_OK;
}

int m2sdr_gain_cal_correction(const struct m2sdr_gain_cal *cal, uint64_t freq_hz, double *gain_corr_db)
{
    size_t lo, hi;

    if (!cal || !gain_corr_db || cal->n_entries == 0)
        return M2SDR_ERR_INVAL;
    const struct m2sdr_gain_cal_entry *e = cal->entries;
    if (freq_hz <= e[0].freq_hz) {
        *gain_corr_db = e[0].gain_corr_db;
        return M2SDR_ERR_OK;
    }
    if (freq_hz >= e[cal->n_entries - 1].freq_hz) {
        *gain_corr_db = e[cal->n_entries - 1].gain_corr_db;
        return M2SDR_ERR_OK;
    }
    /* e[lo].freq < freq <= e[hi].freq */
    lo = 0;
    hi = cal->n_entries - 1;
    while (hi - lo > 1) {
        size_t mid = lo + (hi - lo) / 2;
        if (e[mid].freq_hz < freq_hz)
            lo = mid;
        else
            hi = mid;
    }
    if (e[hi].freq_hz == freq_hz || e[hi].freq_hz == e[lo].freq_hz) {
        *gain_corr_db = e[hi].gain_corr_db;
        return M2SDR_ERR_OK;
    }
    *gain_corr_db = e[lo].gain_corr_db +
                    (double)(freq_hz - e[lo].freq_hz) * (e[hi].gain_corr_db - e[lo].gain_corr_db) /
                    (double)(e[hi].freq_hz - e[lo].freq_hz);
    return M2SDR_ERR_OK;
}

/* Per device --------------------------------------------------------------------------------------*/

int m2sdr_load_rx_gain_calibration(struct m2sdr_dev *dev, unsigned channel, const char *path)
{
    struct m2sdr_gain_cal *cal = NULL;
    int rc;

    if (!dev || channel > 1 || !path)
        return M2SDR_ERR_INVAL;
    rc = m2sdr_gain_cal_load(path, channel, &cal);
    if (rc != M2SDR_ERR_OK)
        return rc;
    m2sdr_gain_cal_free(dev->rx_gain_cal[channel]);
    dev->rx_gain_cal[channel] = cal;
    dev->rx_gain_cal_enabled[channel] = 1;
    return M2SDR_ERR_OK;
}

int m2sdr_unload_rx_gain_calibration(struct m2sdr_dev *dev, unsigned channel)
{
    if (!dev || channel > 1)
        return M2SDR_ERR_INVAL;
    m2sdr_gain_cal_free(dev->rx_gain_cal[channel]);
    dev->rx_gain_cal[channel] = NULL;
    dev->rx_gain_cal_enabled[channel] = 0;
    return M2SDR_ERR_OK;
}

int m2sdr_enable_rx_gain_calibration(struct m2sdr_dev *dev, unsigned channel, bool enable)
{
    if (!dev || channel > 1)
        return M2SDR_ERR_INVAL;
    if (enable && !dev->rx_gain_cal[channel])
        return M2SDR_ERR_STATE;
    dev->rx_gain_cal_enabled[channel] = enable ? 1 : 0;
    return M2SDR_ERR_OK;
}

const struct m2sdr_gain_cal *m2sdr_get_rx_gain_calibration(struct m2sdr_dev *dev, unsigned channel,
                                                           bool *enabled)
{
    if (enabled)
        *enabled = false;
    if (!dev || channel > 1)
        return NULL;
    if (enabled)
        *enabled = dev->rx_gain_cal_enabled[channel] && dev->rx_gain_cal[channel];
    return dev->rx_gain_cal[channel];
}

int m2sdr_rx_gain_db_at(struct m2sdr_dev *dev, unsigned channel, unsigned gain_index,
                        uint64_t rx_freq_hz, double *gain_db, bool *calibrated)
{
    int nominal = 0;
    int rc;

    if (!dev || channel > 1 || !gain_db)
        return M2SDR_ERR_INVAL;
    if (calibrated)
        *calibrated = false;
    rc = m2sdr_rx_gain_index_to_db(dev, gain_index, rx_freq_hz, &nominal);
    if (rc != M2SDR_ERR_OK)
        return rc;
    *gain_db = nominal;
    if (dev->rx_gain_cal_enabled[channel] && dev->rx_gain_cal[channel]) {
        double corr = 0.0;
        rc = m2sdr_gain_cal_correction(dev->rx_gain_cal[channel], rx_freq_hz, &corr);
        if (rc != M2SDR_ERR_OK)
            return rc;
        *gain_db += corr;
        if (calibrated)
            *calibrated = true;
    }
    return M2SDR_ERR_OK;
}
