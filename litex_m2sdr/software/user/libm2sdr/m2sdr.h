/* SPDX-License-Identifier: BSD-2-Clause
 *
 * LiteX-M2SDR public C API (BladeRF-style)
 *
 * Copyright (c) 2024-2026 Enjoy-Digital <enjoy-digital.fr>
 */

#ifndef M2SDR_H
#define M2SDR_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#include "csr.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Thread-safety notes:
 *
 * - A single m2sdr_dev instance is not safe for concurrent calls from multiple
 *   threads without external synchronization.
 * - Independent m2sdr_dev instances can be used concurrently.
 */

/* Public limits and fixed transport sizes used by the sync API. */
#define M2SDR_DEVICE_STR_MAX 256
#define M2SDR_SERIAL_MAX     32
#define M2SDR_IDENT_MAX      256
#define M2SDR_BUFFER_BYTES   8192u
#define M2SDR_HEADER_BYTES   16u
#define M2SDR_BFP8_BLOCK_BYTES 1024u
#define M2SDR_BFP8_PAYLOAD_WORDS 127u
#define M2SDR_BFP8_SAMPLES_PER_CHANNEL 254u

/* Error codes (0 on success, negative on failure).
 *
 * - PARSE: malformed text input (device identifiers, legacy mode strings).
 * - RANGE: numeric value outside supported device/library limits.
 * - STATE: API call ordering/state preconditions not met.
 * - OVERFLOW/UNDERFLOW: streaming ring lost RX/TX continuity.
 */
enum m2sdr_error {
    M2SDR_ERR_OK         =  0,
    M2SDR_ERR_UNEXPECTED = -1,
    M2SDR_ERR_INVAL      = -2,
    M2SDR_ERR_IO         = -3,
    M2SDR_ERR_TIMEOUT    = -4,
    M2SDR_ERR_NO_MEM     = -5,
    M2SDR_ERR_UNSUPPORTED= -6,
    M2SDR_ERR_PARSE      = -7,
    M2SDR_ERR_RANGE      = -8,
    M2SDR_ERR_STATE      = -9,
    M2SDR_ERR_OVERFLOW   = -10,
    M2SDR_ERR_UNDERFLOW  = -11,
};

/* libm2sdr starts its public compatibility story at v1.0.0. Keep the ABI
 * major in sync with the installed shared-library SONAME. */
#define M2SDR_API_VERSION 0x00010201
#define M2SDR_ABI_VERSION 0x00010000
#define M2SDR_VERSION_STRING "1.2.1"

struct m2sdr_version {
    uint32_t api;
    uint32_t abi;
    const char *version_str;
};

/* Return a short static string for a library error code. */
const char *m2sdr_strerror(int err);
/* Fill version information for the linked libm2sdr instance. */
void m2sdr_get_version(struct m2sdr_version *ver);
/* Enable or disable informational library logs. Errors are still reported. */
int m2sdr_set_log_enabled(bool enable);

/* Data direction */
enum m2sdr_direction {
    M2SDR_RX = 0,
    M2SDR_TX = 1,
};

enum m2sdr_transport_kind {
    M2SDR_TRANSPORT_KIND_UNKNOWN = 0,
    M2SDR_TRANSPORT_KIND_LITEPCIE = 1,
    M2SDR_TRANSPORT_KIND_LITEETH = 2,
};

enum m2sdr_sata_dma_direction {
    M2SDR_SATA_DMA_HOST_TO_DEVICE = 0,
    M2SDR_SATA_DMA_DEVICE_TO_HOST = 1,
};

#define M2SDR_SATA_PCIE_DMA_MAX_SECTORS 4096u

/* Backward-compatible alias for older code. */
typedef enum m2sdr_direction m2sdr_module_t;

/* Sample format (subset, extend as needed) */
enum m2sdr_format {
    /* 16-bit I/Q interleaved (SC16 Q11 style) */
    M2SDR_FORMAT_SC16_Q11 = 0,
    /* 8-bit I/Q interleaved (SC8 Q7 style) */
    M2SDR_FORMAT_SC8_Q7  = 1,
    /* Encoded BFP8 blocks: 1x64-bit header + 127x64-bit int8 mantissa payload words. */
    M2SDR_FORMAT_BFP8_Q11 = 2,
};

/* TX air-time quantization. The FPGA time counter advances in M2SDR_TX_TIME_GRID_NS
 * steps (the time-clock period), and hardware timed-TX releases each frame on a grid
 * tick. A TX timestamp is therefore QUANTIZED to this grid: the library rounds it to
 * the nearest multiple before writing it into the DMA header, so the on-air time is a
 * deterministic function of the requested timestamp (round-to-nearest; ties round up). */
#define M2SDR_TX_TIME_GRID_NS 10   /* 100 MHz FPGA time clock -> 10 ns */

struct m2sdr_metadata {
    /* Air-time in ns when M2SDR_META_FLAG_HAS_TIME is set. TX: the requested transmit
     * time, quantized to M2SDR_TX_TIME_GRID_NS (see above). RX: the frame capture time. */
    uint64_t timestamp;
    /* Bitmask of M2SDR_META_FLAG_* values. */
    uint32_t flags;
};

#define M2SDR_META_FLAG_HAS_TIME (1u << 0)

/* Size of the optional FPGA DMA header (sync word + ns timestamp); see the
 * layout description above the zero-copy buffer API below. */
#define M2SDR_DMA_HEADER_SIZE 16

struct m2sdr_sync_params {
    /* RX or TX stream to configure. */
    enum m2sdr_direction direction;
    /* Sample packing for the stream buffers. */
    enum m2sdr_format format;
    /* Backend ring buffer count. Use 0 for library defaults. */
    unsigned num_buffers;
    /* Samples per buffer, not bytes. */
    unsigned buffer_size;
    /* Reserved for API compatibility. Use 0 for defaults. */
    unsigned num_transfers;
    /* Blocking timeout for sync APIs in ms.
     * Passing 0 to per-call timeout arguments means "use this configured
     * timeout", not "non-blocking". */
    unsigned timeout_ms;
    /* Enable zero-copy PCIe DMA buffers when supported. */
    bool zero_copy;
    /* Prefix RX buffers with the FPGA DMA/VRT header. */
    bool rx_header_enable;
    /* Hide the RX header from user buffers when enabled. */
    bool rx_strip_header;
    /* Prefix TX buffers with the FPGA DMA/VRT header. */
    bool tx_header_enable;
};

typedef struct m2sdr_sync_params m2sdr_stream_config_t;

enum m2sdr_liteeth_rx_mode {
    M2SDR_LITEETH_RX_MODE_DISABLED = 0,
    M2SDR_LITEETH_RX_MODE_UDP      = 1,
    M2SDR_LITEETH_RX_MODE_VRT      = 2,
};

struct m2sdr_liteeth_rx_stream_config {
    enum m2sdr_liteeth_rx_mode mode;
    /* Host UDP listen port for raw UDP or VRT RX. */
    uint16_t udp_port;
    /* Host IPv4 address in host byte order. Use 0 for route auto-detection. */
    uint32_t local_ip;
};

struct m2sdr_liteeth_udp_stats {
    size_t buffer_size;
    size_t buffer_count;
    uint64_t rx_buffers;
    uint64_t rx_ring_full_events;
    uint64_t rx_flushes;
    uint64_t rx_flush_bytes;
    uint64_t rx_kernel_drops;
    uint64_t rx_source_drops;
    uint64_t rx_timeout_recoveries;
    uint64_t rx_recv_errors;
    uint64_t tx_buffers;
    uint64_t tx_bytes;
    uint64_t tx_send_errors;
    int so_rcvbuf_requested;
    int so_rcvbuf_actual;
    int so_sndbuf_requested;
    int so_sndbuf_actual;
};

struct m2sdr_stream_stats {
    enum m2sdr_transport_kind transport;
    enum m2sdr_direction direction;
    /* Backend descriptor payload size and ring depth. */
    size_t buffer_size;
    size_t buffer_count;
    /* Raw backend ring counters when available. */
    int64_t hw_count;
    int64_t sw_count;
    /* Current ring occupancy and observed high-water mark in descriptors. */
    uint64_t ring_level;
    uint64_t ring_high_water;
    /* RX late-consumer diagnostics. */
    uint64_t overflow_events;
    uint64_t overflow_buffers;
    /* TX late-producer diagnostics. */
    uint64_t underflow_events;
    uint64_t underflow_buffers;
    /* Transport-specific counters mirrored from LiteEth when available. */
    uint64_t rx_buffers;
    uint64_t tx_buffers;
    uint64_t rx_kernel_drops;
    uint64_t rx_source_drops;
    uint64_t rx_ring_full_events;
    uint64_t rx_timeout_recoveries;
    uint64_t rx_recv_errors;
    uint64_t tx_send_errors;
};

struct m2sdr_stream_info {
    /* Payload bytes visible to the caller, excluding stripped DMA headers. */
    size_t buffer_bytes;
    size_t buffer_count;
    /* Backend stride between direct buffers. May include transport headers. */
    size_t buffer_stride;
    /* Backend-owned direct buffer base when available. */
    void *buffer_base;
};

struct m2sdr_devinfo {
    /* Stable serial string when available. */
    char serial[M2SDR_SERIAL_MAX];
    /* SoC/bitstream identification string. */
    char identification[M2SDR_IDENT_MAX];
    /* Device path or transport address. */
    char path[M2SDR_DEVICE_STR_MAX];
    /* "litepcie" or "liteeth". */
    char transport[16];
};

struct m2sdr_device_addr {
    enum m2sdr_transport_kind transport;
    /* Canonical identifier accepted by m2sdr_open(), eg pcie:/dev/m2sdr0. */
    char identifier[M2SDR_DEVICE_STR_MAX];
    char path[M2SDR_DEVICE_STR_MAX];
    char ip[64];
    uint16_t port;
};

struct m2sdr_discovery_config {
    bool enable_pcie;
    bool enable_liteeth;
    unsigned pcie_first;
    unsigned pcie_count;
    /* Comma- or semicolon-separated list of ip[:port] or eth:ip[:port] targets.
     * NULL selects the library default Ethernet target. */
    const char *liteeth_targets;
    /* Default port for Ethernet targets without an explicit port. Use 0 for
     * the library default. */
    uint16_t liteeth_port;
};

struct m2sdr_discovered_device {
    struct m2sdr_device_addr addr;
    struct m2sdr_devinfo info;
};

struct m2sdr_capabilities {
    /* Gateware API version reported by the FPGA. */
    uint32_t api_version;
    /* Bitmask of M2SDR_FEATURE_* values. */
    uint32_t features;
    uint32_t board_info;
    uint32_t pcie_config;
    uint32_t eth_config;
    uint32_t sata_config;
};

/* Gateware release, read from the FPGA at runtime (Distributed Spectrum images from v1.1.0 on;
 * every change bumps the patch number). Compare versions as integers:
 *     if (gw.known && gw.version >= M2SDR_GATEWARE_VERSION(1, 1, 2)) ...
 * An image built before the register existed reads known = false with every field 0; its build
 * date is still in m2sdr_devinfo.identification. */
#define M2SDR_GATEWARE_VERSION(major, minor, patch) \
    (((uint32_t)(major) << 16) | ((uint32_t)(minor) << 8) | (uint32_t)(patch))

struct m2sdr_gateware_version {
    bool     known;       /* The image reports a gateware version.                       */
    uint32_t version;     /* M2SDR_GATEWARE_VERSION(major, minor, patch).                */
    uint16_t major;
    uint8_t  minor;
    uint8_t  patch;
    uint32_t git_hash;    /* First 8 hex digits of the litex_m2sdr commit (0: unknown).  */
    bool     git_dirty;   /* Built from a tree with uncommitted changes.                 */
    uint64_t build_time;  /* Build time, UTC seconds since 1970 (0: unknown).            */
};

#define M2SDR_SATA_SERIAL_MAX   21
#define M2SDR_SATA_FIRMWARE_MAX 9
#define M2SDR_SATA_MODEL_MAX    41

struct m2sdr_sata_info {
    bool     phy_enabled;         /* SATA PHY enable bit is set.                  */
    uint32_t phy_status;          /* Raw SATA_PHY_STATUS register value.          */
    bool     phy_ready;           /* Decoded from phy_status.                     */
    bool     tx_ready;            /* Decoded from phy_status.                     */
    bool     rx_ready;            /* Decoded from phy_status.                     */
    bool     ctrl_ready;          /* Decoded from phy_status.                     */
    bool     drive_present;       /* identify_done and a non-zero sector count.   */
    bool     identify_done;       /* ATA IDENTIFY completed.                      */
    char     serial[M2SDR_SATA_SERIAL_MAX];     /* NUL-terminated, trimmed.       */
    char     firmware[M2SDR_SATA_FIRMWARE_MAX]; /* NUL-terminated, trimmed.       */
    char     model[M2SDR_SATA_MODEL_MAX];       /* NUL-terminated, trimmed.       */
    uint64_t sector_count;        /* Logical sector count (LBA48, else LBA28).    */
    uint32_t logical_sector_size; /* Bytes per logical sector (512 unless > 512). */
};

struct m2sdr_clock_info {
    /* AD9361 reference clock frequency in Hz. */
    uint64_t refclk_hz;
    /* System clock frequency in Hz when available. */
    uint64_t sysclk_hz;
};

struct m2sdr_ptp_port_identity {
    /* IEEE 1588 clockIdentity and portNumber. Zero values mean unknown. */
    uint64_t clock_id;
    uint16_t port_number;
};

/* Runtime policy for the Ethernet PTP board-time discipline loop. */
struct m2sdr_ptp_discipline_config {
    /* Allow PTP to own and steer the shared board TimeGenerator. */
    bool enable;
    /* Keep the last disciplined increment when PTP lock is lost. */
    bool holdover;
    /* Number of sys_clk cycles between discipline samples. */
    uint32_t update_cycles;
    /* First-lock / large-error threshold that requests an absolute time write. */
    uint32_t coarse_threshold_ns;
    /* Error threshold below which bounded phase trims are preferred. */
    uint32_t phase_threshold_ns;
    /* Error window considered locked after a valid PTP sample. */
    uint32_t lock_window_ns;
    /* Consecutive out-of-window samples required before declaring time unlock. */
    uint32_t unlock_misses;
    /* Consecutive runtime coarse errors required before allowing realignment. */
    uint32_t coarse_confirm;
    /* Right shift applied when converting phase error to phase-trim size. */
    uint8_t phase_step_shift;
    /* Maximum single bounded phase trim in ns. */
    uint32_t phase_step_max_ns;
    /* Signed TimeGenerator increment trim limit around the nominal increment. */
    uint32_t trim_limit;
    /* Proportional gain for TimeGenerator increment steering. */
    uint16_t p_gain;
};

/* Snapshot of Ethernet PTP board-time discipline state and counters. */
struct m2sdr_ptp_status {
    /* Configured enable bit; active means PTP currently owns board time. */
    bool enabled;
    bool active;
    /* ptp_locked tracks packet/reference validity; time_locked tracks local time error. */
    bool ptp_locked;
    bool time_locked;
    /* Holdover means the loop is retaining the last correction after PTP loss. */
    bool holdover;
    uint8_t state;
    /* Master IPv4 address in host byte order. */
    uint32_t master_ip;
    /* Current TimeGenerator fractional increment word. */
    uint32_t time_inc;
    /* Signed PTP time minus local board time for the last discipline sample. */
    int64_t last_error_ns;
    uint64_t last_ptp_time_ns;
    uint64_t last_local_time_ns;
    struct m2sdr_ptp_port_identity local_port;
    struct m2sdr_ptp_port_identity master_port;
    /* Learned sourcePortIdentity update count. */
    uint32_t identity_updates;
    /* Discipline action and loss counters, all wrapping 32-bit values. */
    uint32_t coarse_steps;
    uint32_t phase_steps;
    uint32_t rate_updates;
    uint32_t ptp_lock_losses;
    uint32_t time_lock_misses;
    uint32_t time_lock_miss_count;
    uint32_t time_lock_losses;
};

/* Runtime policy for the PTP-referenced FPGA 10MHz / RFIC clock loop. */
struct m2sdr_ptp_clock10_config {
    /* Allow the clk10 PI loop to override the MMCM dynamic phase-shift rate. */
    bool enable;
    /* Keep the last MMCM rate when the PTP reference is lost. */
    bool holdover;
    /* Invert MMCM phase-step sign when board wiring/MMCM direction requires it. */
    bool invert;
    /* Number of sys_clk cycles between MMCM rate accumulator updates. */
    uint32_t update_cycles;
    /* Q0.32 MMCM steps/update per sys_clk tick of phase error. */
    uint32_t p_gain;
    /* Q0.32 integral gain in MMCM steps/update per sys_clk tick of phase error. */
    uint32_t i_gain;
    /* Absolute Q0.32 MMCM steps/update limit for the PI command. */
    uint32_t rate_limit;
    /* clk10 marker lock window in sys_clk ticks. */
    uint32_t lock_window_ticks;
    /* Phase-detector search window in sys_clk ticks; normally half a second. */
    uint32_t half_period_ticks;
};

/* Snapshot of the PTP-to-10MHz discipline loop state and counters. */
struct m2sdr_ptp_clock10_status {
    /* Configured enable bit; active means the PTP clk10 loop owns MMCM steering. */
    bool enabled;
    bool active;
    /* reference_locked follows the board-time PTP reference. */
    bool reference_locked;
    /* clock_locked means the clk10 marker is inside lock_window_ticks. */
    bool clock_locked;
    bool holdover;
    /* aligned is set after automatic or manual 1Hz marker alignment. */
    bool aligned;
    /* Phase detector is waiting for the clk10 marker following a PTP reference. */
    bool waiting_after_ref;
    /* Most recent PI update saturated at rate_limit. */
    bool rate_limited;
    /* sys_clk phase-detector tick period in picoseconds. */
    uint32_t phase_tick_ps;
    /* Signed clk10 marker minus PTP reference phase error. */
    int32_t last_error_ticks;
    int64_t last_error_ns;
    /* Signed Q0.32 MMCM steps/update command. */
    int32_t last_rate;
    /* Phase-detector and PI-loop counters, all wrapping 32-bit values. */
    uint32_t sample_count;
    uint32_t reference_count;
    uint32_t clk10_count;
    uint32_t missing_count;
    uint32_t align_count;
    uint32_t lock_loss_count;
    uint32_t rate_update_count;
    uint32_t saturation_count;
};

enum m2sdr_feature_flag {
#ifdef CSR_CAPABILITY_FEATURES_PCIE_OFFSET
    M2SDR_FEATURE_PCIE = 1u << CSR_CAPABILITY_FEATURES_PCIE_OFFSET,
#else
    M2SDR_FEATURE_PCIE = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_ETH_OFFSET
    M2SDR_FEATURE_ETH  = 1u << CSR_CAPABILITY_FEATURES_ETH_OFFSET,
#else
    M2SDR_FEATURE_ETH  = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_SATA_OFFSET
    M2SDR_FEATURE_SATA = 1u << CSR_CAPABILITY_FEATURES_SATA_OFFSET,
#else
    M2SDR_FEATURE_SATA = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_GPIO_OFFSET
    M2SDR_FEATURE_GPIO = 1u << CSR_CAPABILITY_FEATURES_GPIO_OFFSET,
#else
    M2SDR_FEATURE_GPIO = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_WR_OFFSET
    M2SDR_FEATURE_WR   = 1u << CSR_CAPABILITY_FEATURES_WR_OFFSET,
#else
    M2SDR_FEATURE_WR   = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_JTAGBONE_OFFSET
    M2SDR_FEATURE_JTAGBONE = 1u << CSR_CAPABILITY_FEATURES_JTAGBONE_OFFSET,
#else
    M2SDR_FEATURE_JTAGBONE = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_ETH_PTP_OFFSET
    M2SDR_FEATURE_ETH_PTP = 1u << CSR_CAPABILITY_FEATURES_ETH_PTP_OFFSET,
#else
    M2SDR_FEATURE_ETH_PTP = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_ETH_PTP_RFIC_CLOCK_OFFSET
    M2SDR_FEATURE_ETH_PTP_RFIC_CLOCK = 1u << CSR_CAPABILITY_FEATURES_ETH_PTP_RFIC_CLOCK_OFFSET,
#else
    M2SDR_FEATURE_ETH_PTP_RFIC_CLOCK = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_TIMED_TX_OFFSET
    M2SDR_FEATURE_TIMED_TX = 1u << CSR_CAPABILITY_FEATURES_TIMED_TX_OFFSET,
#else
    M2SDR_FEATURE_TIMED_TX = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_RFIC_OVERSAMPLING_OFFSET
    M2SDR_FEATURE_RFIC_OVERSAMPLING = 1u << CSR_CAPABILITY_FEATURES_RFIC_OVERSAMPLING_OFFSET,
#else
    M2SDR_FEATURE_RFIC_OVERSAMPLING = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_RX_GAIN_TAG_OFFSET
    M2SDR_FEATURE_RX_GAIN_TAG = 1u << CSR_CAPABILITY_FEATURES_RX_GAIN_TAG_OFFSET,
#else
    M2SDR_FEATURE_RX_GAIN_TAG = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_GPS_OFFSET
    M2SDR_FEATURE_GPS = 1u << CSR_CAPABILITY_FEATURES_GPS_OFFSET,
#else
    M2SDR_FEATURE_GPS = 0,
#endif
};

enum m2sdr_feature_mask {
#ifdef CSR_CAPABILITY_FEATURES_PCIE_SIZE
    M2SDR_FEATURE_PCIE_MASK = ((1u << CSR_CAPABILITY_FEATURES_PCIE_SIZE) - 1u) << CSR_CAPABILITY_FEATURES_PCIE_OFFSET,
#else
    M2SDR_FEATURE_PCIE_MASK = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_ETH_SIZE
    M2SDR_FEATURE_ETH_MASK  = ((1u << CSR_CAPABILITY_FEATURES_ETH_SIZE)  - 1u) << CSR_CAPABILITY_FEATURES_ETH_OFFSET,
#else
    M2SDR_FEATURE_ETH_MASK  = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_SATA_SIZE
    M2SDR_FEATURE_SATA_MASK = ((1u << CSR_CAPABILITY_FEATURES_SATA_SIZE) - 1u) << CSR_CAPABILITY_FEATURES_SATA_OFFSET,
#else
    M2SDR_FEATURE_SATA_MASK = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_GPIO_SIZE
    M2SDR_FEATURE_GPIO_MASK = ((1u << CSR_CAPABILITY_FEATURES_GPIO_SIZE) - 1u) << CSR_CAPABILITY_FEATURES_GPIO_OFFSET,
#else
    M2SDR_FEATURE_GPIO_MASK = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_WR_SIZE
    M2SDR_FEATURE_WR_MASK   = ((1u << CSR_CAPABILITY_FEATURES_WR_SIZE)   - 1u) << CSR_CAPABILITY_FEATURES_WR_OFFSET,
#else
    M2SDR_FEATURE_WR_MASK   = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_JTAGBONE_SIZE
    M2SDR_FEATURE_JTAGBONE_MASK = ((1u << CSR_CAPABILITY_FEATURES_JTAGBONE_SIZE) - 1u) << CSR_CAPABILITY_FEATURES_JTAGBONE_OFFSET,
#else
    M2SDR_FEATURE_JTAGBONE_MASK = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_ETH_PTP_SIZE
    M2SDR_FEATURE_ETH_PTP_MASK =
        ((1u << CSR_CAPABILITY_FEATURES_ETH_PTP_SIZE) - 1u) << CSR_CAPABILITY_FEATURES_ETH_PTP_OFFSET,
#else
    M2SDR_FEATURE_ETH_PTP_MASK = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_ETH_PTP_RFIC_CLOCK_SIZE
    M2SDR_FEATURE_ETH_PTP_RFIC_CLOCK_MASK =
        ((1u << CSR_CAPABILITY_FEATURES_ETH_PTP_RFIC_CLOCK_SIZE) - 1u) <<
        CSR_CAPABILITY_FEATURES_ETH_PTP_RFIC_CLOCK_OFFSET,
#else
    M2SDR_FEATURE_ETH_PTP_RFIC_CLOCK_MASK = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_TIMED_TX_SIZE
    M2SDR_FEATURE_TIMED_TX_MASK = ((1u << CSR_CAPABILITY_FEATURES_TIMED_TX_SIZE) - 1u) << CSR_CAPABILITY_FEATURES_TIMED_TX_OFFSET,
#else
    M2SDR_FEATURE_TIMED_TX_MASK = 0,
#endif
#ifdef CSR_CAPABILITY_FEATURES_RFIC_OVERSAMPLING_SIZE
    M2SDR_FEATURE_RFIC_OVERSAMPLING_MASK = ((1u << CSR_CAPABILITY_FEATURES_RFIC_OVERSAMPLING_SIZE) - 1u) << CSR_CAPABILITY_FEATURES_RFIC_OVERSAMPLING_OFFSET,
#else
    M2SDR_FEATURE_RFIC_OVERSAMPLING_MASK = 0,
#endif
};

struct m2sdr_fpga_sensors {
    double temperature_c;
    double vccint_v;
    double vccaux_v;
    double vccbram_v;
};

enum m2sdr_channel_layout {
    M2SDR_CHANNEL_LAYOUT_2T2R = 0,
    M2SDR_CHANNEL_LAYOUT_1T1R = 1,
};

enum m2sdr_clock_source {
    M2SDR_CLOCK_SOURCE_INTERNAL      = 0,
    M2SDR_CLOCK_SOURCE_EXTERNAL      = 1,
    M2SDR_CLOCK_SOURCE_SI5351C_FPGA  = 2,
};

enum m2sdr_rx_gain_mode {
    M2SDR_RX_GAIN_MODE_MANUAL = 0,
    M2SDR_RX_GAIN_MODE_SLOW_ATTACK_AGC = 1,
    M2SDR_RX_GAIN_MODE_FAST_ATTACK_AGC = 2,
    M2SDR_RX_GAIN_MODE_HYBRID_AGC = 3,
};

enum m2sdr_agc_detector {
    M2SDR_AGC_DETECTOR_RX1_LOW = 0,
    M2SDR_AGC_DETECTOR_RX1_HIGH = 1,
    M2SDR_AGC_DETECTOR_RX2_LOW = 2,
    M2SDR_AGC_DETECTOR_RX2_HIGH = 3,
};

struct m2sdr_agc_counter_config {
    bool enable;
    bool clear;
    uint16_t threshold;
};

/* Maximum accepted |refclk_ppm| reference clock correction. */
#define M2SDR_REFCLK_PPM_MAX 100.0

/* RF configuration (matches existing utilities defaults) */
struct m2sdr_config {
    /* Common TX/RX sample rate in SPS. */
    int64_t sample_rate;
    /* Common TX/RX RF bandwidth in Hz. */
    int64_t bandwidth;
    /* AD9361 reference clock in Hz. */
    int64_t refclk_freq;
    /* Measured reference clock error in ppm (positive = clock runs fast).
     * Compensated by trimming the SI5351 PLL feedback multiplier, so the
     * AD9361 reference and all derived clocks are corrected together. */
    double  refclk_ppm;
    /* TX LO in Hz. */
    int64_t tx_freq;
    /* RX LO in Hz. */
    int64_t rx_freq;
    /* TX attenuation in dB. Higher values mean lower output power. */
    int64_t tx_att;
    /* RX gain for channel 0 in dB. */
    int64_t rx_gain1;
    /* RX gain for channel 1 in dB. */
    int64_t rx_gain2;
    /* Apply explicit RX gain programming and force manual gain mode. Enabled
     * by m2sdr_config_init(); clear this and set program_rx_gain_modes when
     * using AD9361 AGC modes instead. */
    bool    program_rx_gains;
    /* Apply explicit AD9361 RX gain-control modes when not programming manual gains. */
    bool    program_rx_gain_modes;
    enum m2sdr_rx_gain_mode rx_gain_mode1;
    enum m2sdr_rx_gain_mode rx_gain_mode2;
    /* Drive the FPGA-connected AD9361 EN_AGC pin for pin-controlled AGC flows. */
    bool    program_agc_pin;
    bool    agc_pin_enable;
    uint8_t loopback;
    bool    bist_tx_tone;
    bool    bist_rx_tone;
    bool    bist_prbs;
    /* Scan/program FPGA <-> AD9361 interface delays using the PRBS path. */
    bool    calibrate_interface_delay;
    int32_t bist_tone_freq;
    bool    enable_8bit_mode;
    enum m2sdr_format sample_format;
    /* Preferred typed RF topology controls. */
    enum m2sdr_channel_layout channel_layout;
    enum m2sdr_clock_source clock_source;
    /* Legacy string overrides kept for compatibility with old utilities. */
    const char *chan_mode; /* legacy string override: "2t2r" or "1t1r" */
    const char *sync_mode; /* legacy string override: "internal", "external", or "fpga" */
};

struct m2sdr_dev;

/* Device management.
 *
 * Device identifiers are:
 * - "pcie:/dev/m2sdr0"
 * - "eth:192.168.1.50:1234"
 *
 * The parser also accepts the shorthand forms "/dev/m2sdr0" and
 * "192.168.1.50:1234". Passing NULL selects the backend default.
 */
int  m2sdr_resolve_device_identifier(const char *device_identifier,
                                      struct m2sdr_device_addr *addr);
int  m2sdr_open(struct m2sdr_dev **dev, const char *device_identifier);
void m2sdr_close(struct m2sdr_dev *dev);

/* Initialize discovery policy with the library defaults:
 * PCIe /dev/m2sdr0..7 followed by Ethernet target 192.168.1.50:1234. */
void m2sdr_discovery_config_init(struct m2sdr_discovery_config *config);

/* Expand a discovery policy into canonical candidate identifiers. This does
 * not open devices; use m2sdr_get_device_list_ex() to probe reachability. */
int  m2sdr_get_discovery_targets(const struct m2sdr_discovery_config *config,
                                 struct m2sdr_device_addr *targets,
                                 size_t max,
                                 size_t *count);

/* Enumerate reachable devices for a configured discovery policy.
 *
 * Discovery is bounded to the configured PCIe indices and Ethernet targets; it
 * never performs a subnet scan.
 */
int  m2sdr_get_device_list_ex(const struct m2sdr_discovery_config *config,
                              struct m2sdr_discovered_device *list,
                              size_t max,
                              size_t *count);
int  m2sdr_get_device_list(struct m2sdr_devinfo *list, size_t max, size_t *count);

/* Read stable transport/path/serial/identifier metadata from an open device. */
int  m2sdr_get_device_info(struct m2sdr_dev *dev, struct m2sdr_devinfo *info);
int  m2sdr_get_capabilities(struct m2sdr_dev *dev, struct m2sdr_capabilities *caps);
/* Gateware release and build provenance (struct m2sdr_gateware_version). M2SDR_ERR_OK with
 * known = false on an image that predates the version register. */
int  m2sdr_get_gateware_version(struct m2sdr_dev *dev, struct m2sdr_gateware_version *version);
int  m2sdr_get_identifier(struct m2sdr_dev *dev, char *buf, size_t len);
int  m2sdr_get_fpga_git_hash(struct m2sdr_dev *dev, uint32_t *hash);
int  m2sdr_get_clock_info(struct m2sdr_dev *dev, struct m2sdr_clock_info *info);
int  m2sdr_get_sata_info(struct m2sdr_dev *dev, struct m2sdr_sata_info *info, unsigned timeout_ms);

/* Register access.
 *
 * These are intentionally exposed for board-specific tooling, but most
 * applications should prefer higher-level helpers.
 */
int  m2sdr_reg_read(struct m2sdr_dev *dev, uint32_t addr, uint32_t *val);
int  m2sdr_reg_write(struct m2sdr_dev *dev, uint32_t addr, uint32_t val);
int  m2sdr_reg_read_bulk(struct m2sdr_dev *dev, uint32_t addr, uint32_t *vals, size_t count);
int  m2sdr_reg_write_bulk(struct m2sdr_dev *dev, uint32_t addr, const uint32_t *vals, size_t count);

/* Copy SATA sectors to/from host memory using the LitePCIe userspace DMA path.
 *
 * 'buf' must hold at least nsectors * 512 bytes. 'timeout_ms' bounds each DMA
 * chunk (< 0 waits forever). On return '*transferred' (if non-NULL) holds the
 * number of sectors actually copied, even on error, so a partial transfer can
 * be resumed. Returns M2SDR_ERR_UNSUPPORTED on non-PCIe transports.
 */
int  m2sdr_sata_pcie_dma_copy(struct m2sdr_dev *dev,
                              enum m2sdr_sata_dma_direction direction,
                              uint64_t sector,
                              uint32_t nsectors,
                              void *buf,
                              int timeout_ms,
                              uint32_t *transferred);

/* Low-level transport handle access for advanced integrations.
 *
 * `m2sdr_get_fd()` returns a valid descriptor only for LitePCIe devices.
 * `m2sdr_get_eb_handle()` returns a valid handle only for LiteEth devices.
 * `m2sdr_get_transport()` is the preferred way to branch on active backend.
 * `m2sdr_get_handle()` returns either the PCIe file descriptor cast to `void *`
 * or the Etherbone connection handle, depending on the active backend. On
 * LitePCIe devices this value is an integer fd reinterpreted as a pointer; it
 * is only for passing through opaque APIs and must not be dereferenced. Prefer
 * backend-specific accessors in new code.
 */
int  m2sdr_get_fd(struct m2sdr_dev *dev);
void *m2sdr_get_eb_handle(struct m2sdr_dev *dev);
int  m2sdr_get_transport(struct m2sdr_dev *dev, enum m2sdr_transport_kind *transport);
void *m2sdr_get_handle(struct m2sdr_dev *dev);

/* DMA header control */
int  m2sdr_set_rx_header(struct m2sdr_dev *dev, bool enable, bool strip_header);
int  m2sdr_set_tx_header(struct m2sdr_dev *dev, bool enable);

/* GPIO helper (4-bit) */
int  m2sdr_gpio_config(struct m2sdr_dev *dev, bool enable, bool loopback, bool source_csr);
int  m2sdr_gpio_write(struct m2sdr_dev *dev, uint8_t value, uint8_t oe);
int  m2sdr_gpio_read(struct m2sdr_dev *dev, uint8_t *value);

/* Time */
int  m2sdr_get_time(struct m2sdr_dev *dev, uint64_t *time_ns);
int  m2sdr_set_time(struct m2sdr_dev *dev, uint64_t time_ns);

/* GPS time input (gateware built with --with-gps, capability M2SDR_FEATURE_GPS).
 *
 * A u-blox receiver (Gateworks GW16143 ZED-F9P on the M.2 breakout) sends UBX-NAV-TIMEUTC on its
 * UART1 to the FPGA (Synchro_GPIO2/3) and its timepulse to PPS_IN. The gateware decodes the UTC
 * second, qualifies the PPS (glitch filter + 0.9 s refractory window), and can load the board time
 * with (S + 1) * 1e9 + sync_offset ns on the PPS that starts second S + 1. RX header timestamps,
 * m2sdr_get_time() and timed TX then all run on GPS UTC (ns since 1970), with no host clock in the
 * path. The F9P must be provisioned (over its USB port) to output NAV-TIMEUTC on UART1 at the
 * gateware baud (115200 by default) with its timepulse on the UTC top of the second.
 *
 * Between syncs the board time runs on the SI5351-derived 100 MHz clock: pps_time_ns, latched on
 * every qualified PPS before any sync, measures that clock against GPS. While syncing every PPS,
 * pps_time_ns - (sync_epoch + 1) * 1e9 - sync_offset_ns is the error the second accumulated (the
 * size of the step the sync applied); m2sdr_set_refclk_ppm() trims the SI5351 (and with it the
 * AD9361 sample clock) to drive it toward zero. */
enum m2sdr_gps_sync_mode {
    M2SDR_GPS_SYNC_OFF       = 0, /* Decode and monitor only; the board time is not touched. */
    M2SDR_GPS_SYNC_EVERY_PPS = 1, /* Load the board time on every qualified PPS with fresh UTC. */
    M2SDR_GPS_SYNC_ONCE      = 2, /* Load it on the next such PPS, then free-run.               */
};

struct m2sdr_gps_status {
    bool     utc_valid;       /* Last good NAV-TIMEUTC had validUTC and in-range fields.     */
    bool     utc_fresh;       /* A valid decode within the last 1.5 s.                       */
    bool     pps_alive;       /* A qualified PPS within the last 1.5 s.                      */
    bool     pending;         /* A valid decode is waiting for the next PPS.                 */
    bool     synced;          /* The last qualified PPS loaded the board time.               */
    bool     once_armed;      /* A one-shot sync is armed.                                   */
    bool     sync_every;      /* Sync mode is M2SDR_GPS_SYNC_EVERY_PPS.                       */
    bool     time_sync_enable;/* The time generator accepts syncs (time_gen sync_enable).    */
    bool     pps_bypass;      /* The PPS pin is ignored (m2sdr_gps_set_pps_bypass()).        */
    uint64_t epoch;           /* UTC seconds of the last valid decode.                       */
    uint32_t t_acc_ns;        /* Time accuracy estimate of the last good frame.              */
    int32_t  nano_ns;         /* nano field of the last good frame.                          */
    uint64_t pps_time_ns;     /* Board time latched on the last qualified PPS (before sync). */
    uint64_t sync_epoch;      /* S of the last sync: board time = (S + 1) * 1e9 + offset.     */
    uint32_t sync_offset_ns;  /* PPS pin -> time write latency compensation.                 */
    uint32_t baudrate;        /* UART baud from the tuning word.                             */
    uint32_t msg_count;       /* Good NAV-TIMEUTC frames.                                    */
    uint32_t ck_errors;       /* NAV-TIMEUTC frames with a bad checksum.                     */
    uint32_t pps_count;       /* Qualified PPS edges.                                        */
    uint32_t pps_rejected;    /* PPS edges ignored inside the refractory window.             */
    uint32_t sync_count;      /* Syncs applied.                                              */
    uint32_t pps_no_utc;      /* Qualified PPS edges with no fresh valid decode pending.     */
};

/* All return M2SDR_ERR_UNSUPPORTED on gateware without the GPS input. */
int  m2sdr_gps_get_status(struct m2sdr_dev *dev, struct m2sdr_gps_status *status);
/* Select the sync mode; any mode other than OFF also sets the time generator's sync_enable
 * (M2SDR_ERR_STATE while PTP owns the board time). */
int  m2sdr_gps_set_sync(struct m2sdr_dev *dev, enum m2sdr_gps_sync_mode mode);
int  m2sdr_gps_set_baudrate(struct m2sdr_dev *dev, uint32_t baudrate);
int  m2sdr_gps_set_sync_offset(struct m2sdr_dev *dev, uint32_t offset_ns);
int  m2sdr_gps_clear_counters(struct m2sdr_dev *dev);
/* PPS bypass: with bypass = true the gateware ignores the PPS pin. No PPS is qualified, latched,
 * counted or used to sync, so the board time free-runs (whatever the sync mode); UBX decoding
 * continues. For a PPS line that is missing, noisy or not trusted. Off at power-up/reload; kept
 * across opens and by the other m2sdr_gps_* calls. The pps_in CSRs still see the pin. The
 * bladeRF_DS gps_uart image has the same switch (bladerf_set_gps_pps_bypass()). */
int  m2sdr_gps_set_pps_bypass(struct m2sdr_dev *dev, bool bypass);
int  m2sdr_get_ptp_status(struct m2sdr_dev *dev, struct m2sdr_ptp_status *status);
int  m2sdr_get_ptp_discipline_config(struct m2sdr_dev *dev, struct m2sdr_ptp_discipline_config *cfg);
int  m2sdr_set_ptp_discipline_config(struct m2sdr_dev *dev, const struct m2sdr_ptp_discipline_config *cfg);
int  m2sdr_clear_ptp_counters(struct m2sdr_dev *dev);
int  m2sdr_get_ptp_clock10_status(struct m2sdr_dev *dev, struct m2sdr_ptp_clock10_status *status);
int  m2sdr_get_ptp_clock10_config(struct m2sdr_dev *dev, struct m2sdr_ptp_clock10_config *cfg);
int  m2sdr_set_ptp_clock10_config(struct m2sdr_dev *dev, const struct m2sdr_ptp_clock10_config *cfg);
int  m2sdr_clear_ptp_clock10_counters(struct m2sdr_dev *dev);
/* Queue a clk10 marker realignment; hardware applies it on the next PTP reference edge. */
int  m2sdr_align_ptp_clock10(struct m2sdr_dev *dev);
int  m2sdr_set_agc_pin(struct m2sdr_dev *dev, bool enable);
int  m2sdr_get_agc_pin(struct m2sdr_dev *dev, bool *enabled);
int  m2sdr_configure_agc_counter(struct m2sdr_dev *dev,
                                 enum m2sdr_agc_detector detector,
                                 const struct m2sdr_agc_counter_config *config);
int  m2sdr_clear_agc_counter(struct m2sdr_dev *dev, enum m2sdr_agc_detector detector);
int  m2sdr_get_agc_count(struct m2sdr_dev *dev, enum m2sdr_agc_detector detector,
                         uint32_t *count);
int  m2sdr_set_sample_format(struct m2sdr_dev *dev, enum m2sdr_format format);
int  m2sdr_set_bitmode(struct m2sdr_dev *dev, bool enable_8bit);
int  m2sdr_set_dma_loopback(struct m2sdr_dev *dev, bool enable);
int  m2sdr_set_txrx_loopback(struct m2sdr_dev *dev, bool enable);
int  m2sdr_set_rfic_data_loopback(struct m2sdr_dev *dev, bool enable);
int  m2sdr_get_rfic_data_loopback(struct m2sdr_dev *dev, bool *enabled);
int  m2sdr_reset_datapath(struct m2sdr_dev *dev);
int  m2sdr_get_fpga_dna(struct m2sdr_dev *dev, uint64_t *dna);
int  m2sdr_get_fpga_sensors(struct m2sdr_dev *dev, struct m2sdr_fpga_sensors *sensors);

/* RF config */
/* Initialize cfg with sane defaults matching the shipped utilities.
 *
 * This is the recommended starting point before overriding only the fields
 * your application cares about.
 */
void m2sdr_config_init(struct m2sdr_config *cfg);
/* Apply a full RF configuration to the currently-open device.
 *
 * This performs the same high-level bring-up sequence as `m2sdr_rf`: SI5351
 * clock selection, AD9361 SPI/RF init, channel layout, rates, gains, loopback,
 * and optional BIST modes.
 */
int  m2sdr_apply_config(struct m2sdr_dev *dev, const struct m2sdr_config *cfg);
/* Apply cfg only when no RF config has been applied yet, or when cfg matches
 * the last successfully applied config. This avoids accidental AD9361 re-init
 * while letting startup code call the helper idempotently. Returns
 * M2SDR_ERR_STATE when the RFIC is already initialized with a different config. */
int  m2sdr_apply_config_if_needed(struct m2sdr_dev *dev,
                                  const struct m2sdr_config *cfg);

/* Direction-based RF setters.
 *
 * These assume the AD9361 has already been initialized either through
 * `m2sdr_apply_config()` or an advanced integration path such as Soapy.
 */
int  m2sdr_set_frequency(struct m2sdr_dev *dev, enum m2sdr_direction direction, uint64_t freq);
int  m2sdr_set_sample_rate(struct m2sdr_dev *dev, int64_t rate);
int  m2sdr_set_bandwidth(struct m2sdr_dev *dev, int64_t bw);
/* Trim the SI5351 PLL to compensate a measured reference clock error in ppm
 * (positive = reference runs fast). The correction is always relative to the
 * nominal clock tree and pulls all SI5351-derived clocks together, without
 * resetting the PLL, so it can track a slowly drifting reference at runtime.
 * Requires a prior m2sdr_apply_config() so the active clock topology is
 * known. */
int  m2sdr_set_refclk_ppm(struct m2sdr_dev *dev, double ppm);
/* Last reference trim applied through the RF config or m2sdr_set_refclk_ppm(). */
int  m2sdr_get_refclk_ppm(struct m2sdr_dev *dev, double *ppm);
/* Glitch-free retrim window of the active clock topology in ppm, clamped to
 * +-M2SDR_REFCLK_PPM_MAX. Retrims inside it move only the PLL fractional
 * field; outside, the feedback is re-centred and that one update is not
 * glitch-free. */
int  m2sdr_get_refclk_ppm_window(struct m2sdr_dev *dev, double *ppm_min, double *ppm_max);
int  m2sdr_set_gain(struct m2sdr_dev *dev, enum m2sdr_direction direction, int64_t gain);
/* Switch the 1T1R/2T2R channel layout. Re-initializes the AD9361 from the
 * shared init parameters (the device must have completed RF bring-up once),
 * going through the same configuration code as the init-time path. Callers
 * caching the phy handle must refresh it via m2sdr_rf_phy() afterwards, and
 * re-apply their RF settings (rates, frequencies, gains). */
int  m2sdr_set_channel_mode(struct m2sdr_dev *dev, unsigned channel_count,
                            unsigned rx_channel, unsigned tx_channel);
const char *m2sdr_rx_gain_mode_name(enum m2sdr_rx_gain_mode mode);
int  m2sdr_parse_rx_gain_mode(const char *text, enum m2sdr_rx_gain_mode *mode);
int  m2sdr_set_rx_gain_mode_all(struct m2sdr_dev *dev, enum m2sdr_rx_gain_mode mode);
/* Stream sample rate as observed by the host (inverse of the conversions
 * applied by m2sdr_set_sample_rate()). */
int  m2sdr_get_sample_rate(struct m2sdr_dev *dev, int64_t *rate);
/* Currently bound AD9361 phy handle (NULL when uninitialized). */
void *m2sdr_rf_phy(struct m2sdr_dev *dev);
int  m2sdr_set_rx_gain_mode(struct m2sdr_dev *dev, unsigned channel,
                            enum m2sdr_rx_gain_mode mode);
int  m2sdr_get_rx_gain_mode(struct m2sdr_dev *dev, unsigned channel,
                            enum m2sdr_rx_gain_mode *mode);
/* Convenience RF setters for the common one-direction case. */
int  m2sdr_set_rx_frequency(struct m2sdr_dev *dev, uint64_t freq);
int  m2sdr_set_tx_frequency(struct m2sdr_dev *dev, uint64_t freq);
int  m2sdr_set_rx_gain(struct m2sdr_dev *dev, int64_t gain);
int  m2sdr_set_rx_gain_chan(struct m2sdr_dev *dev, unsigned channel, int64_t gain);
/* The RX gain (dB) in force on channel, read from the AD9361: the manual gain, or what the AGC
 * chose. M2SDR_ERR_STATE when the RX path is disabled or fast-attack AGC has not locked yet. */
int  m2sdr_get_rx_gain_chan(struct m2sdr_dev *dev, unsigned channel, int64_t *gain);
int  m2sdr_set_tx_att(struct m2sdr_dev *dev, int64_t attenuation_db);
int  m2sdr_set_rfic_loopback(struct m2sdr_dev *dev, uint8_t mode);
int  m2sdr_set_fpga_prbs_tx(struct m2sdr_dev *dev, bool enable);
int  m2sdr_get_fpga_prbs_rx_synced(struct m2sdr_dev *dev, bool *synced);
/* Advanced integration hook used by the SoapySDR driver. */
int  m2sdr_rf_bind(struct m2sdr_dev *dev, void *ad9361_phy);
/* Scan and program the FPGA <-> AD9361 interface clock/data delays with the
 * PRBS path (same procedure as m2sdr_rf --calibrate-delay). Requires an
 * initialized RFIC and an idle datapath: the scan corrupts any active stream,
 * and on gateware without a 1R1T-aware PRBS checker it only converges in 2T2R
 * mode. The selected delays are programmed immediately and persisted in the
 * stored AD9361 init parameters, so later RFIC re-initializations (e.g.
 * channel-mode changes) program them again instead of reverting to the
 * init-table defaults. */
int  m2sdr_rf_calibrate_interface_delay(struct m2sdr_dev *dev);
/* Store the AD9361_InitParam an external integration initialized the RFIC
 * with, so m2sdr_set_channel_mode() re-initializes with the same reference
 * clock, SPI id and GPIO setup. init_param must point to an AD9361_InitParam
 * and size must be sizeof(AD9361_InitParam) (guards ABI mismatches). */
int  m2sdr_rf_store_init_param(struct m2sdr_dev *dev,
                               const void *init_param, size_t size);

/* Streaming (BladeRF-like sync API) */
/* Configure a stream directly.
 *
 * buffer_size is the fixed backend descriptor payload size expressed in
 * samples per buffer, not the size of each m2sdr_sync_rx()/m2sdr_sync_tx()
 * request. For BFP8, one sample is one encoded M2SDR_BFP8_BLOCK_BYTES block.
 * Use
 * m2sdr_bytes_to_samples(M2SDR_FORMAT_..., M2SDR_BUFFER_BYTES)
 * for the default DMA payload size. Larger per-call requests are passed to
 * m2sdr_sync_rx()/m2sdr_sync_tx(), which drain/fill multiple descriptors.
 */
int m2sdr_sync_config(struct m2sdr_dev *dev,
                      enum m2sdr_direction direction,
                      enum m2sdr_format format,
                      unsigned num_buffers,
                      unsigned buffer_size,
                      unsigned num_transfers,
                      unsigned timeout_ms);

/* Initialize the extended stream config with library defaults. */
void m2sdr_sync_params_init(struct m2sdr_sync_params *params);
/* Alias of m2sdr_sync_params_init() for the public stream-config name. */
void m2sdr_stream_config_init(m2sdr_stream_config_t *config);
/* Configure a stream with header/zero-copy options. */
int m2sdr_sync_config_ex(struct m2sdr_dev *dev, const struct m2sdr_sync_params *params);
/* Alias of m2sdr_sync_config_ex() for the public stream-config name. */
int m2sdr_stream_configure(struct m2sdr_dev *dev, const m2sdr_stream_config_t *config);
int m2sdr_stream_get_info(struct m2sdr_dev *dev,
                          enum m2sdr_direction direction,
                          struct m2sdr_stream_info *info);
int m2sdr_get_stream_stats(struct m2sdr_dev *dev,
                           enum m2sdr_direction direction,
                           struct m2sdr_stream_stats *stats);
int m2sdr_clear_stream_stats(struct m2sdr_dev *dev,
                             enum m2sdr_direction direction);
int m2sdr_stream_deactivate(struct m2sdr_dev *dev, enum m2sdr_direction direction);
/* Re-enable a previously configured (and possibly deactivated) stream. On
 * LitePCIe this resyncs the userspace ring counters with the kernel, whose
 * counters restart across a stop/start cycle. */
int m2sdr_stream_activate(struct m2sdr_dev *dev, enum m2sdr_direction direction);
/* RX start alignment (LitePCIe). The gateware's DMA synchronizer holds a started RX stream until
 * the next PPS of the board time, and the driver re-arms it on every RX start, so by default the
 * first buffer after m2sdr_stream_activate(RX) arrives on the next whole second: up to 1 s later.
 * With pps_aligned = false, later RX activations bypass the synchronizer and RX starts at once
 * (each buffer still carries its timestamp when the RX header is on). The driver clears the
 * bypass whenever RX stops, so it is re-applied on every activation. Returns
 * M2SDR_ERR_UNSUPPORTED on a transport or gateware without the synchronizer. */
int m2sdr_set_rx_start_pps_aligned(struct m2sdr_dev *dev, bool pps_aligned);
int m2sdr_stream_release(struct m2sdr_dev *dev, enum m2sdr_direction direction);

/* LiteEth stream-control helpers.
 *
 * These functions are additive helpers for integrations that need explicit
 * setup/activate/deactivate control instead of the blocking sync API.
 */
void m2sdr_liteeth_rx_stream_config_init(struct m2sdr_liteeth_rx_stream_config *config);
int m2sdr_liteeth_get_local_ip(struct m2sdr_dev *dev, uint32_t *local_ip);
int m2sdr_liteeth_rx_stream_prepare(struct m2sdr_dev *dev,
                                    const struct m2sdr_liteeth_rx_stream_config *config);
int m2sdr_liteeth_rx_stream_activate(struct m2sdr_dev *dev,
                                     const struct m2sdr_liteeth_rx_stream_config *config);
int m2sdr_liteeth_rx_stream_deactivate(struct m2sdr_dev *dev);
int m2sdr_liteeth_tx_stream_activate(struct m2sdr_dev *dev);
int m2sdr_liteeth_tx_stream_deactivate(struct m2sdr_dev *dev);
int m2sdr_liteeth_set_rx_timeout_recovery(struct m2sdr_dev *dev, bool enable);
int m2sdr_liteeth_get_udp_stats(struct m2sdr_dev *dev,
                                struct m2sdr_liteeth_udp_stats *stats);

/* Blocking sync receive/transmit helpers.
 *
 * timeout_ms = 0 uses the stream timeout configured through
 * m2sdr_sync_config()/m2sdr_sync_config_ex().
 *
 * num_samples is the requested transfer size and may be larger than the
 * descriptor size configured with m2sdr_sync_config().
 *
 * When RX headers are enabled, meta carries the hardware timestamp of the
 * FIRST sample of the returned block (a request spanning several DMA buffers
 * keeps the first buffer's timestamp). */
int m2sdr_sync_rx(struct m2sdr_dev *dev,
                  void *samples,
                  unsigned num_samples,
                  struct m2sdr_metadata *meta,
                  unsigned timeout_ms);

int m2sdr_sync_tx(struct m2sdr_dev *dev,
                  const void *samples,
                  unsigned num_samples,
                  struct m2sdr_metadata *meta,
                  unsigned timeout_ms);

/* Set the timed-TX pipeline offset (ns). The hardware timed-TX gate releases each timed
 * frame the cycle (FPGA time + tx_offset) reaches the frame's air-time, so tx_offset
 * compensates the fixed TX-pipeline latency (packer/CDC/serializer/DAC/analog) between the
 * gate and the antenna: calibrated, "transmit at X" puts the signal on the air at X.
 * Untimed frames (header timestamp 0) always transmit immediately, unaffected by this.
 * Loopback-calibrated. LitePCIe only; may be set before or during streaming. */
int m2sdr_set_tx_offset(struct m2sdr_dev *dev, uint64_t offset_ns);

/* Read the running hardware TX underflow count: timed frames that missed their air-time.
 * A timed frame whose air-time had already passed when it reached the gate is dropped
 * whole (the RFIC airs zeros for it) and counted here. A steadily rising value means timed
 * frames are submitted later than their air-times (submit earlier or check tx_offset).
 * This is the on-air TX underflow measured at the gate; the m2sdr_get_stats() underflow
 * fields count the separate DMA-ring underflow (reader starved of any buffer). LitePCIe. */
int m2sdr_get_tx_underflow(struct m2sdr_dev *dev, uint32_t *underflow);

/* Set the RX timestamp offset (ns): the mirror of tx_offset. The RX header inserter stamps
 * each buffer when it starts forming (DMA plane); rx_offset is subtracted so the stamp
 * refers to when the first sample was at the ANTENNA. With both offsets calibrated, TX and
 * RX timestamps share one reference plane (needed for absolute/multi-board timing; TX<->RX
 * self-consistency alone is already given by the loopback-calibrated tx_offset, which
 * absorbs the sum of both pipelines). 0 (default) keeps the historical DMA-plane stamping.
 * LitePCIe only. */
int m2sdr_set_rx_offset(struct m2sdr_dev *dev, uint64_t offset_ns);

/* Zero-copy buffer API.
 *
 * These helpers expose backend-owned buffers directly. RX buffers must be
 * returned with m2sdr_release_buffer(); TX buffers must be submitted with
 * m2sdr_submit_buffer(). Passing timeout_ms=0 uses the timeout configured by
 * m2sdr_sync_config()/m2sdr_sync_config_ex(). m2sdr_try_get_buffer() is
 * non-blocking and returns M2SDR_ERR_TIMEOUT when no buffer is immediately
 * available.
 *
 * DMA header layout (when enabled):
 * - Total size: 16 bytes.
 * - Bytes [0..7]: sync word 0x5aa55aa55aa55aa5.
 * - Bytes [8..15]: timestamp in nanoseconds.
 * - Endianness: values are encoded/decoded in native little-endian byte order
 *   as used by current host/FPGA flows.
 */
/* Set the zero-copy TX fill lead, in buffers, ahead of the DMA reader cursor (LitePCIe).
 *
 * The zero-copy TX path paces how far ahead of the free-running reader the host fills the ring;
 * that lead x the per-buffer air-time IS the TX pipeline latency. Because the reader loops with
 * no backpressure, the host must stay far enough ahead that a submitted frame is not overrun
 * before the reader reaches it.
 *
 *  - lead_buffers == 0: legacy behavior -- fill the whole ring (maximum throughput, and the
 *    maximum ~ring-depth pipeline latency). Best for continuous streaming.
 *  - lead_buffers  > 0: hold the host this many buffers ahead of the IRQ-updated reader cursor;
 *    latency drops to ~lead x buffer air-time. It MUST be strictly greater than the kernel's
 *    dma_buffer_per_irq (the IRQ-coalescing depth): the fill paces against the IRQ-updated
 *    cursor, which the free-running reader can run up to dma_buffer_per_irq buffers ahead of,
 *    so a lead at or below that lets the reader overtake a just-filled slot and air stale
 *    samples with no error signal. With the low-latency insmod (dma_buffer_per_irq=2) use
 *    lead >= 3 (nonzero values are floored to 3); with the default coalescing
 *    (dma_buffer_per_irq=8) such a small lead is not meaningful -- use 0. The consumer must
 *    also be real-time: a stall longer than the lead underflows the reader.
 *
 * Clamped to the ring depth. Takes effect on the next m2sdr_get_buffer(TX). Returns M2SDR_ERR_OK
 * on a LitePCIe device, M2SDR_ERR_UNSUPPORTED otherwise. */
int m2sdr_set_tx_lead_buffers(struct m2sdr_dev *dev, unsigned lead_buffers);

int m2sdr_get_buffer(struct m2sdr_dev *dev,
                     enum m2sdr_direction direction,
                     void **buffer,
                     unsigned *num_samples,
                     unsigned timeout_ms);

int m2sdr_try_get_buffer(struct m2sdr_dev *dev,
                         enum m2sdr_direction direction,
                         void **buffer,
                         unsigned *num_samples);

int m2sdr_submit_buffer(struct m2sdr_dev *dev,
                        enum m2sdr_direction direction,
                        void *buffer,
                        unsigned num_samples,
                        struct m2sdr_metadata *meta);

int m2sdr_release_buffer(struct m2sdr_dev *dev,
                         enum m2sdr_direction direction,
                         void *buffer);

/* Retrieve the metadata carried by the DMA header of an RX buffer obtained
 * from m2sdr_get_buffer()/m2sdr_try_get_buffer(). meta->flags carries
 * M2SDR_META_FLAG_HAS_TIME (with meta->timestamp in ns) only when RX headers
 * are enabled and the buffer holds a valid sync word; otherwise meta is
 * cleared and the call still succeeds, so it can be issued unconditionally. */
int m2sdr_get_buffer_metadata(struct m2sdr_dev *dev,
                              enum m2sdr_direction direction,
                              const void *buffer,
                              struct m2sdr_metadata *meta);

/* RX gain tag.
 *
 * Gateware built with the RX gain tag (capability M2SDR_FEATURE_RX_GAIN_TAG) samples the AD9361
 * CTRL_OUT pins with every RX sample word and, when the tag is enabled, replaces the DMA header's
 * sync word with the RX1 full gain-table index over the frame that follows (the same idea as the
 * bladeRF_DS per-message gain tag). A frame is one DMA buffer's payload. The 8 header bytes are:
 *
 *   bits [15: 0]  0x5aa5 sync          bits [30:24]  BASE: index at the frame's first word
 *   bits [19:16]  version (1)          bits [38:32]  END0: index at the last word of chunk 0
 *   bit  [20]     VALID                bits [46:40]  END1    bits [54:48]  END2
 *   bit  [21]     LOCK (CTRL_OUT[7])   bits [62:56]  END3 (= the frame's last word)
 *   bit  [22]     CHANGED (index moved inside the frame)
 *
 * Chunks are counted in 64-bit stream WORDS (2 SC16 samples, 4 SC8 samples, a 2T2R sample pair
 * in SC16): with W payload words, chunk c is words [ceil(c*W/4), ceil((c+1)*W/4)). Each chunk end
 * is the word holding sample (c+1)*S/4 - 1 of the S-sample frame, so in sample terms the chunks
 * are the bladeRF ones (S/4 samples each: 511 for a 1T1R SC16 frame of 2044).
 * m2sdr_rx_gain_tag_chunk() does this mapping from a sample position.
 *
 * The CTRL_OUT pins show one channel's gain at a time (row 0x16 RX1, 0x17 RX2), so the tag
 * describes the channel m2sdr_set_rx_gain_tag() selected: the one a 1T1R stream carries. In 2T2R
 * the other channel is untagged. The index is the whole gain state
 * in the AD9361 full gain-table mode with digital gain off (the libm2sdr defaults); convert it to
 * dB at the frame's RX frequency with m2sdr_rx_gain_index_to_db(). The legacy 0x5aa55aa55aa55aa5
 * word decodes as version 5, so the two never collide; m2sdr_sync_rx() and
 * m2sdr_get_buffer_metadata() accept both. */
#define M2SDR_RX_GAIN_TAG_VERSION_1     1
#define M2SDR_RX_GAIN_TAG_CHUNKS        4
#define M2SDR_RX_GAIN_TAG_FLAG_VALID    (1u << 0) /* Every word carried a settled CTRL_OUT byte.  */
#define M2SDR_RX_GAIN_TAG_FLAG_LOCK     (1u << 1) /* CTRL_OUT[7] (gain lock) at the first word.   */
#define M2SDR_RX_GAIN_TAG_FLAG_CHANGED  (1u << 2) /* The index moved inside the frame.            */

struct m2sdr_rx_gain_tag {
    uint64_t timestamp;   /* Header timestamp (ns) of the frame's first sample.              */
    uint32_t flags;       /* M2SDR_RX_GAIN_TAG_FLAG_*.                                        */
    uint8_t  version;     /* M2SDR_RX_GAIN_TAG_VERSION_1.                                     */
    uint8_t  gain_index;  /* BASE: index at the frame's first word.                           */
    uint8_t  gain_index_min; /* Min / max over BASE and the chunk ends.                       */
    uint8_t  gain_index_max;
    uint8_t  chunk_gain_index[M2SDR_RX_GAIN_TAG_CHUNKS]; /* Index at the end of each chunk.    */
};

/* Decode one 16-byte DMA header. Returns 1 for a gain tag (tag filled), 0 for the legacy sync
 * word (no tag; tag cleared), M2SDR_ERR_PARSE for anything else. */
int m2sdr_parse_rx_gain_tag(const void *header, struct m2sdr_rx_gain_tag *tag);

/* Enable or disable the tag. Enabling points the AD9361 CTRL_OUT mux at channel's gain row
 * (0 = RX1: row 0x16, 1 = RX2: row 0x17, lock + index; needs the RF layer bound) and sets the
 * HEADER rx_gain_tag CSR. Takes effect on frames formed after the call: change it only while the
 * RX stream is stopped. The header must also be enabled (m2sdr_set_rx_header()).
 * M2SDR_ERR_UNSUPPORTED on gateware without the tag. */
int m2sdr_set_rx_gain_tag(struct m2sdr_dev *dev, bool enable, unsigned channel);
/* enabled: the CSR is set. active: the gateware is tagging (enabled + header on + frame fits). */
int m2sdr_get_rx_gain_tag(struct m2sdr_dev *dev, bool *enabled, bool *active);

/* Per-frame gain tags behind the samples of the most recent m2sdr_sync_rx() (the analogue of
 * bladerf_get_rx_gain_tags()). Each entry describes one DMA buffer's slice of the returned
 * block: samples [sample_offset, sample_offset + sample_count) of the block are samples
 * [msg_sample_offset, msg_sample_offset + sample_count) of a frame of msg_samples samples.
 * Entries tile the block in order. Frames whose header carried no tag are skipped, so a gap in
 * the tiling means "no gain known there". *num_tags may exceed max_tags: only max_tags are
 * copied. M2SDR_ERR_UNSUPPORTED when the tag is not enabled on this device. */
struct m2sdr_rx_gain_tag_msg {
    struct m2sdr_rx_gain_tag tag;
    uint32_t sample_offset;
    uint32_t sample_count;
    uint32_t msg_sample_offset;
    uint32_t msg_samples;
};
int m2sdr_get_rx_gain_tags(struct m2sdr_dev *dev,
                           struct m2sdr_rx_gain_tag_msg *tags,
                           size_t max_tags,
                           size_t *num_tags);

/* Gain tag of an RX buffer from m2sdr_get_buffer()/m2sdr_try_get_buffer() (zero-copy path).
 * Returns 1 with tag filled, 0 when the buffer's header carries no tag (or headers are off). */
int m2sdr_get_buffer_rx_gain_tag(struct m2sdr_dev *dev,
                                 const void *buffer,
                                 struct m2sdr_rx_gain_tag *tag);

/* Chunk (0..3) holding sample sample_in_msg of a frame of msg_samples samples in format. */
unsigned m2sdr_rx_gain_tag_chunk(enum m2sdr_format format, unsigned msg_samples, unsigned sample_in_msg);

/* AD9361 RX gain (dB) of full gain-table index gain_index at RX LO rx_freq_hz: the same table
 * m2sdr_set_rx_gain() programs from, so a manual gain of G dB reads back as G. Pass the LO the
 * frame was captured at (not the LO now: a retune changes the band, and one index then means a
 * different gain). Needs the RF layer bound. M2SDR_ERR_RANGE when the index is past the band's
 * table (usually: CTRL_OUT is not on row 0x16). */
int m2sdr_rx_gain_index_to_db(struct m2sdr_dev *dev, unsigned gain_index,
                              uint64_t rx_freq_hz, int *gain_db);

/* RX gain calibration tables (dBFS -> dBm).
 *
 * The AD9361 table gain is nominal: what a capture's full-scale-relative power (dBFS) means in
 * dBm at the antenna port also depends on the board's front end at that frequency. A per-board
 * table, measured with a signal generator into the RX port and stepped across the LO range,
 * gives the correction, in the same files the bladeRF tooling writes: the CSV
 * ("Serial: <serial>" + the bladeRF RX header + rows chain,gain,dBm_in,f_sig,f_lo,rssi,dBFS)
 * or its .tbl (libbladeRF image) form, detected by content. Each row yields
 *
 *     gain_corr(f_lo) = (dBFS - dBm_in) - G_commanded
 *
 * (the bladeRF tables are swept at G = 0, so this is libbladeRF's gain_corr), interpolated
 * linearly in LO and clamped at the table ends. With the nominal gain G of the gain index in force
 * (m2sdr_rx_gain_index_to_db(), or a gain tag's index):
 *
 *     dBm_in = dBFS - (G + gain_corr(f_lo))       // m2sdr_rx_gain_db_at() returns G + gain_corr
 *
 * Channels: 0 = RX1, 1 = RX2. Rows are matched by RX chain in libbladeRF channel numbering
 * (RX1 = 0, RX2 = 2), or plain indices (RX2 = 1) when the table has no chain-2 rows; when a
 * chain was swept at several gains, the rows at its first gain are used. */
#define M2SDR_GAIN_CAL_SERIAL_MAX 33
#define M2SDR_GAIN_CAL_PATH_MAX   256

struct m2sdr_gain_cal;           /* Opaque; usable without a device (offline conversion). */

struct m2sdr_gain_cal_entry {
    uint64_t freq_hz;            /* LO frequency of the measurement. */
    double   gain_corr_db;       /* (dBFS - dBm_in) - G at that LO.   */
};

struct m2sdr_gain_cal_info {
    unsigned channel;
    uint32_t n_entries;
    int32_t  gain_db;            /* Commanded gain the rows were measured at. */
    uint64_t start_freq_hz;
    uint64_t stop_freq_hz;
    char     serial[M2SDR_GAIN_CAL_SERIAL_MAX];
    char     path[M2SDR_GAIN_CAL_PATH_MAX];
};

/* Load channel's rows from a .csv or .tbl table. M2SDR_ERR_PARSE for a file that is neither an
 * RX gain CSV nor an RX .tbl, M2SDR_ERR_RANGE when it has no rows for the channel. */
int  m2sdr_gain_cal_load(const char *path, unsigned channel, struct m2sdr_gain_cal **cal);
void m2sdr_gain_cal_free(struct m2sdr_gain_cal *cal);
int  m2sdr_gain_cal_get_info(const struct m2sdr_gain_cal *cal, struct m2sdr_gain_cal_info *info);
int  m2sdr_gain_cal_get_entries(const struct m2sdr_gain_cal *cal,
                                const struct m2sdr_gain_cal_entry **entries, size_t *n_entries);
/* gain_corr (dB) at an LO frequency: linear between rows, clamped outside the table. */
int  m2sdr_gain_cal_correction(const struct m2sdr_gain_cal *cal, uint64_t freq_hz, double *gain_corr_db);

/* Attach a table to an RX channel of a device (replacing any; enabled on load). */
int  m2sdr_load_rx_gain_calibration(struct m2sdr_dev *dev, unsigned channel, const char *path);
int  m2sdr_unload_rx_gain_calibration(struct m2sdr_dev *dev, unsigned channel);
/* M2SDR_ERR_STATE when enabling a channel with no table. */
int  m2sdr_enable_rx_gain_calibration(struct m2sdr_dev *dev, unsigned channel, bool enable);
/* The channel's table (NULL when none); *enabled tells whether it is applied. */
const struct m2sdr_gain_cal *m2sdr_get_rx_gain_calibration(struct m2sdr_dev *dev, unsigned channel,
                                                           bool *enabled);
/* Total RX gain (dB) of gain_index at rx_freq_hz on channel: the AD9361 table gain plus, when the
 * channel's table is enabled, gain_corr(rx_freq_hz) (*calibrated says which). dBm = dBFS - gain_db.
 * The bladerf_rx_gain_tag_to_gain_db_at() counterpart. Pass the LO the samples were captured at. */
int  m2sdr_rx_gain_db_at(struct m2sdr_dev *dev, unsigned channel, unsigned gain_index,
                         uint64_t rx_freq_hz, double *gain_db, bool *calibrated);

/* Backend notes for zero-copy submit/release:
 *
 * - m2sdr_submit_buffer() performs an explicit enqueue on LiteEth and advances
 *   the kernel-visible TX ring on LitePCIe zero-copy streams.
 * - m2sdr_release_buffer() advances the kernel-visible RX ring on LitePCIe
 *   zero-copy streams; other current paths advance on read.
 */

/* RX DMA ring -> file descriptor, with no copy (LitePCIe zero-copy RX streams).
 *
 * Copies runs of consecutive DMA ring buffers (up to chunk_buffers, never across the ring wrap)
 * into a page-aligned RAM queue (queue_bytes) and releases them at once; a writer thread writes
 * each queued run to the fd with one write(). A slow write (an SSD's housekeeping stalls) then
 * fills the queue instead of overrunning the DMA ring. The queue copy is the only one: the fd
 * can be opened with O_DIRECT to bypass the page cache (O_DIRECT cannot read the driver's
 * PFN-mapped ring itself). A final partial buffer (max_bytes not a multiple of 4096) is written
 * with O_DIRECT cleared. Headers, if enabled, stay in the data.
 *
 * Requires a stream configured with m2sdr_sync_config_ex() and zero_copy set. Runs until
 * max_bytes are written (0: no limit), *keep_running reads 0, or a write fails. Overruns
 * (writer more than half a ring behind) resync to the live writer like m2sdr_get_buffer() and
 * leave a gap in the file; they are counted in the stats and in m2sdr_get_stream_stats(). */
struct m2sdr_rx_to_fd_stats {
    uint64_t bytes;             /* Bytes written to fd.                                       */
    uint64_t buffers;           /* DMA buffers written.                                       */
    uint64_t writes;            /* write() calls.                                              */
    uint64_t overflow_events;   /* Overruns: resyncs after falling over half a ring behind.  */
    uint64_t overflow_buffers;  /* Buffers skipped by those resyncs (gaps in the file).       */
    uint64_t torn_writes;       /* Writes the DMA may have overwritten (lapped) while writing. */
    uint64_t ring_high_water;   /* Deepest backlog, in buffers.                               */
    uint64_t max_write_us;      /* Slowest single write().                                     */
    uint64_t queue_high_water;  /* Most RAM queue bytes waiting for the disk.                 */
};

struct m2sdr_rx_to_fd_params {
    int fd;                         /* Output; O_DIRECT recommended.                          */
    uint64_t max_bytes;             /* Stop after this many bytes (0: no limit).             */
    unsigned chunk_buffers;         /* DMA buffers per write (0: 256); at most half the ring. */
    const volatile int *keep_running; /* Optional: stop once it reads 0.                       */
    void (*progress)(const struct m2sdr_rx_to_fd_stats *stats, void *user); /* Optional, ~1 Hz. */
    void *user;
    size_t queue_bytes;             /* RAM queue between ring and disk (0: 256 MiB).         */
};

int m2sdr_rx_stream_to_fd(struct m2sdr_dev *dev,
                          const struct m2sdr_rx_to_fd_params *params,
                          struct m2sdr_rx_to_fd_stats *stats);

/* Trim the free space of the filesystem holding path, as fstrim does, so an SSD can rebuild its
 * dynamic write cache before the next capture (run it after deleting old captures; the cache
 * refills while the drive idles). Uses the FITRIM ioctl when the caller has CAP_SYS_ADMIN
 * (trimmed_bytes, if given, is then set); otherwise runs "sudo -n fstrim <mountpoint>", which
 * needs a NOPASSWD sudoers rule for fstrim. Returns M2SDR_ERR_IO if neither works. */
int m2sdr_fstrim(const char *path, uint64_t *trimmed_bytes);

/* Small utility helpers for buffer sizing and allocation. */
size_t m2sdr_format_size(enum m2sdr_format format);
size_t m2sdr_samples_to_bytes(enum m2sdr_format format, unsigned num_samples);
unsigned m2sdr_bytes_to_samples(enum m2sdr_format format, size_t num_bytes);
void  *m2sdr_alloc_buffer(enum m2sdr_format format, unsigned num_samples);
void   m2sdr_free_buffer(void *buf);

#ifdef __cplusplus
}
#endif

#endif /* M2SDR_H */
