/* SPDX-License-Identifier: BSD-2-Clause
 *
 * LiteX-M2SDR library
 *
 * This file is part of LiteX-M2SDR.
 *
 * Copyright (c) 2024-2026 Enjoy-Digital <enjoy-digital.fr>
 *
 */

/* Includes */
/*----------*/

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#include <linux/fs.h>

#include "m2sdr.h"

extern char **environ;

/* Utility helpers */
/*-----------------*/

size_t m2sdr_format_size(enum m2sdr_format format)
{
    /* Keep the size table local here so all higher layers share one canonical
     * definition of each public sample format. */
    switch (format) {
    case M2SDR_FORMAT_SC16_Q11:
        return 4;
    case M2SDR_FORMAT_SC8_Q7:
        return 2;
    case M2SDR_FORMAT_BFP8_Q11:
        return M2SDR_BFP8_BLOCK_BYTES;
    default:
        return 0;
    }
}

size_t m2sdr_samples_to_bytes(enum m2sdr_format format, unsigned num_samples)
{
    size_t sample_size = m2sdr_format_size(format);
    if (!sample_size || num_samples == 0)
        return 0;
    return sample_size * num_samples;
}

unsigned m2sdr_bytes_to_samples(enum m2sdr_format format, size_t num_bytes)
{
    size_t sample_size = m2sdr_format_size(format);
    if (!sample_size || (num_bytes % sample_size) != 0)
        return 0;
    return (unsigned)(num_bytes / sample_size);
}

void *m2sdr_alloc_buffer(enum m2sdr_format format, unsigned num_samples)
{
    size_t bytes = m2sdr_samples_to_bytes(format, num_samples);
    if (!bytes)
        return NULL;
    void *buf = NULL;
    /* 64-byte alignment keeps DMA/SIMD-friendly callers out of trouble without
     * exposing transport-specific alignment rules in the public API. */
    if (posix_memalign(&buf, 64, bytes) != 0)
        return NULL;
    return buf;
}

void m2sdr_free_buffer(void *buf)
{
    free(buf);
}

/* Mount point of the filesystem holding path: walk up while the device stays the same. */
static int m2sdr_mount_point(const char *path, char *mnt, size_t mnt_len)
{
    char cur[PATH_MAX];
    struct stat st, parent_st;

    if (!realpath(path, cur))
        return -1;
    if (stat(cur, &st) != 0)
        return -1;
    if (!S_ISDIR(st.st_mode)) {
        char *slash = strrchr(cur, '/');
        if (!slash)
            return -1;
        if (slash == cur)
            slash[1] = '\0';
        else
            *slash = '\0';
        if (stat(cur, &st) != 0)
            return -1;
    }
    while (strcmp(cur, "/") != 0) {
        char parent[PATH_MAX];
        char *slash;

        snprintf(parent, sizeof(parent), "%s", cur);
        slash = strrchr(parent, '/');
        if (slash == parent)
            parent[1] = '\0';
        else
            *slash = '\0';
        if (stat(parent, &parent_st) != 0 || parent_st.st_dev != st.st_dev)
            break;
        snprintf(cur, sizeof(cur), "%s", parent);
    }
    if (strlen(cur) >= mnt_len)
        return -1;
    memcpy(mnt, cur, strlen(cur) + 1);
    return 0;
}

int m2sdr_fstrim(const char *path, uint64_t *trimmed_bytes)
{
    char mnt[PATH_MAX];
    struct fstrim_range range;
    int fd;

    if (trimmed_bytes)
        *trimmed_bytes = 0;
    if (!path || m2sdr_mount_point(path, mnt, sizeof(mnt)) != 0)
        return M2SDR_ERR_INVAL;

    /* Direct FITRIM (what fstrim does): needs CAP_SYS_ADMIN. */
    fd = open(mnt, O_RDONLY | O_DIRECTORY);
    if (fd < 0)
        return M2SDR_ERR_IO;
    memset(&range, 0, sizeof(range));
    range.len = UINT64_MAX;
    if (ioctl(fd, FITRIM, &range) == 0) {
        close(fd);
        if (trimmed_bytes)
            *trimmed_bytes = range.len;
        return M2SDR_ERR_OK;
    }
    int err = errno;
    close(fd);
    if (err != EPERM && err != EACCES)
        return err == EOPNOTSUPP ? M2SDR_ERR_UNSUPPORTED : M2SDR_ERR_IO;

    /* Unprivileged: "sudo -n fstrim <mount>", which needs a NOPASSWD sudoers rule for fstrim;
     * -n makes it fail instead of prompting. */
    char *argv[] = { "sudo", "-n", "fstrim", mnt, NULL };
    pid_t pid;
    int status = 0;
    if (posix_spawnp(&pid, "sudo", NULL, NULL, argv, environ) != 0)
        return M2SDR_ERR_IO;
    if (waitpid(pid, &status, 0) < 0 || !WIFEXITED(status) || WEXITSTATUS(status) != 0)
        return M2SDR_ERR_IO;
    return M2SDR_ERR_OK;
}
