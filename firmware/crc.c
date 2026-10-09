/*
 * crc.c —— CRC 校验的 C 实现（嵌入式侧）
 *
 * 为什么在 C 里再实现一遍？
 * ------------------------
 * 上位机（Python）和设备固件（C）必须算出**完全相同**的校验值，
 * 否则一个认为帧是好的、另一个认为坏了。这种"两端算法不一致"
 * 是联调时最难查的问题之一：现象是"偶发丢帧"，但两边单独测都对。
 *
 * 所以这个文件的输出会被上位机逐字节比对（tests/test_c_conformance.py），
 * 保证两套实现的算法、掩码、字节序完全一致。
 *
 * 嵌入式约束
 * ----------
 * 这里的写法是按"能跑在单片机上"来写的：
 *   - 不用动态内存、不用浮点；
 *   - 查表法把 CRC-8 的 256 项预先算好放在 const 段（Flash），
 *     避免每字节 8 次循环；
 *   - CRC-16 逐位算，省掉 512 字节表——在只有几 KB RAM 的
 *     51 单片机上，用时间换空间是常规取舍。
 *
 * 编译（任意 C89 以上编译器）：
 *     cc -O2 -o crc_cli crc.c
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ── CRC-8/MAXIM ─────────────────────────────────────────── */

/*
 * 表放在 const 段：单片机上会被放进 Flash 而不是 RAM。
 * 用函数生成表也可以，但预生成能省掉启动时间，也让代码更直观。
 */
static const unsigned char CRC8_TABLE[256] = {
    0x00,0x5E,0xBC,0xE2,0x61,0x3F,0xDD,0x83,0xC2,0x9C,0x7E,0x20,0xA3,0xFD,0x1F,0x41,
    0x9D,0xC3,0x21,0x7F,0xFC,0xA2,0x40,0x1E,0x5F,0x01,0xE3,0xBD,0x3E,0x60,0x82,0xDC,
    0x23,0x7D,0x9F,0xC1,0x42,0x1C,0xFE,0xA0,0xE1,0xBF,0x5D,0x03,0x80,0xDE,0x3C,0x62,
    0xBE,0xE0,0x02,0x5C,0xDF,0x81,0x63,0x3D,0x7C,0x22,0xC0,0x9E,0x1D,0x43,0xA1,0xFF,
    0x46,0x18,0xFA,0xA4,0x27,0x79,0x9B,0xC5,0x84,0xDA,0x38,0x66,0xE5,0xBB,0x59,0x07,
    0xDB,0x85,0x67,0x39,0xBA,0xE4,0x06,0x58,0x19,0x47,0xA5,0xFB,0x78,0x26,0xC4,0x9A,
    0x65,0x3B,0xD9,0x87,0x04,0x5A,0xB8,0xE6,0xA7,0xF9,0x1B,0x45,0xC6,0x98,0x7A,0x24,
    0xF8,0xA6,0x44,0x1A,0x99,0xC7,0x25,0x7B,0x3A,0x64,0x86,0xD8,0x5B,0x05,0xE7,0xB9,
    0x8C,0xD2,0x30,0x6E,0xED,0xB3,0x51,0x0F,0x4E,0x10,0xF2,0xAC,0x2F,0x71,0x93,0xCD,
    0x11,0x4F,0xAD,0xF3,0x70,0x2E,0xCC,0x92,0xD3,0x8D,0x6F,0x31,0xB2,0xEC,0x0E,0x50,
    0xAF,0xF1,0x13,0x4D,0xCE,0x90,0x72,0x2C,0x6D,0x33,0xD1,0x8F,0x0C,0x52,0xB0,0xEE,
    0x32,0x6C,0x8E,0xD0,0x53,0x0D,0xEF,0xB1,0xF0,0xAE,0x4C,0x12,0x91,0xCF,0x2D,0x73,
    0xCA,0x94,0x76,0x28,0xAB,0xF5,0x17,0x49,0x08,0x56,0xB4,0xEA,0x69,0x37,0xD5,0x8B,
    0x57,0x09,0xEB,0xB5,0x36,0x68,0x8A,0xD4,0x95,0xCB,0x29,0x77,0xF4,0xAA,0x48,0x16,
    0xE9,0xB7,0x55,0x0B,0x88,0xD6,0x34,0x6A,0x2B,0x75,0x97,0xC9,0x4A,0x14,0xF6,0xA8,
    0x74,0x2A,0xC8,0x96,0x15,0x4B,0xA9,0xF7,0xB6,0xE8,0x0A,0x54,0xD7,0x89,0x6B,0x35
};

unsigned char crc8_maxim(const unsigned char *data, size_t len)
{
    unsigned char crc = 0x00;
    size_t i;
    for (i = 0; i < len; i++) {
        crc = CRC8_TABLE[crc ^ data[i]];
    }
    return crc;
}

/* ── CRC-16/MODBUS ───────────────────────────────────────── */

/*
 * 逐位计算，不查表。
 * 代价是每字节 8 次移位，收益是省下 512 字节常量表——
 * 在 RAM/Flash 紧张的 8 位机上是划算的。
 */
unsigned short crc16_modbus(const unsigned char *data, size_t len)
{
    unsigned short crc = 0xFFFF;
    size_t i;
    int bit;

    for (i = 0; i < len; i++) {
        crc ^= (unsigned short)data[i];
        for (bit = 0; bit < 8; bit++) {
            if (crc & 0x0001) {
                crc = (unsigned short)((crc >> 1) ^ 0xA001);
            } else {
                crc = (unsigned short)(crc >> 1);
            }
        }
    }
    return crc;
}

/*
 * 按 Modbus 线上字节序写入：低字节在前。
 * 单独抽一个函数，是因为"算对了但字节序写反"是最常见的翻车点。
 */
void crc16_modbus_bytes(const unsigned char *data, size_t len, unsigned char out[2])
{
    unsigned short crc = crc16_modbus(data, len);
    out[0] = (unsigned char)(crc & 0xFF);        /* 低字节在前 */
    out[1] = (unsigned char)((crc >> 8) & 0xFF);
}

/* ── 11 位通道打包（与 Python 侧对应）─────────────────────── */

#define BITS_PER_CHANNEL 11
#define CHANNEL_COUNT    16
#define SWITCH_COUNT     2
#define TOTAL_CHANNELS   (CHANNEL_COUNT + SWITCH_COUNT)
#define PACKED_SIZE      ((TOTAL_CHANNELS * BITS_PER_CHANNEL + 7) / 8)  /* 25 */

/*
 * 把通道值按 11 位紧凑打包。
 * 与 Python 的 encode_dbus() 必须逐字节一致。
 */
void pack_channels(const unsigned short *channels, size_t n,
                   unsigned char *out, size_t out_len)
{
    size_t bit = 0;
    size_t i;
    int b;

    memset(out, 0, out_len);
    for (i = 0; i < n; i++) {
        unsigned short v = (unsigned short)(channels[i] & 0x07FF); /* 11 位掩码 */
        for (b = 0; b < BITS_PER_CHANNEL; b++) {
            if ((v >> b) & 1u) {
                size_t byte_i = bit >> 3;
                if (byte_i < out_len) {
                    out[byte_i] |= (unsigned char)(1u << (bit & 7));
                }
            }
            bit++;
        }
    }
}

/*
 * 按位取反——DBUS 电平反相的软件修正方案。
 * 硬件上则串一个 74HC04 反相器。
 */
void invert_bytes(unsigned char *data, size_t len)
{
    size_t i;
    for (i = 0; i < len; i++) {
        data[i] = (unsigned char)(data[i] ^ 0xFF);
    }
}

/* ── 命令行：给上位机调用做一致性比对 ────────────────────── */

static void print_hex(const unsigned char *data, size_t len)
{
    size_t i;
    for (i = 0; i < len; i++) {
        printf("%02x", data[i]);
    }
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* 把命令行参数当成 hex 字符串解析成字节 */
static size_t parse_hex(const char *s, unsigned char *out, size_t max)
{
    size_t n = 0;
    while (s[0] && s[1] && n < max) {
        int hi = hexval(s[0]);
        int lo = hexval(s[1]);
        if (hi < 0 || lo < 0) break;
        out[n++] = (unsigned char)((hi << 4) | lo);
        s += 2;
    }
    return n;
}

static int usage(void)
{
    fprintf(stderr,
        "用法:\n"
        "  crc_cli crc16 <hex>       CRC-16/MODBUS 整数值（4 位十六进制）\n"
        "  crc_cli crc16b <hex>      CRC-16/MODBUS 线上字节序（低字节在前）\n"
        "  crc_cli crc8 <hex>        CRC-8/MAXIM（2 位十六进制）\n"
        "  crc_cli pack <v0,v1,...>  按 11 位打包通道值 -> hex\n"
        "  crc_cli invert <hex>      按位取反 -> hex\n"
        "  crc_cli frame <hex>       帧结构: a55a + len + payload + crc16(小端)\n");
    return 2;
}

/*
 * strdup 不在 C99 标准里（属于 POSIX / C23），
 * 在 gcc -std=c99 下会报 implicit declaration（若开 -Werror 直接编译失败）。
 * 自己实现一个，保证严格 C99 可编译——这也是嵌入式项目里的常见做法，
 * 因为交叉编译工具链的标准库支持程度参差不齐。
 */
static char *dup_string(const char *s)
{
    size_t n = strlen(s) + 1;
    char *p = (char *)malloc(n);
    if (p != NULL) {
        memcpy(p, s, n);
    }
    return p;
}

int main(int argc, char **argv)
{
    unsigned char buf[4096];
    size_t len;

    if (argc < 3) return usage();

    if (strcmp(argv[1], "crc16") == 0) {
        len = parse_hex(argv[2], buf, sizeof buf);
        printf("%04x\n", crc16_modbus(buf, len));
    } else if (strcmp(argv[1], "crc16b") == 0) {
        unsigned char out[2];
        len = parse_hex(argv[2], buf, sizeof buf);
        crc16_modbus_bytes(buf, len, out);
        print_hex(out, 2);
        printf("\n");
    } else if (strcmp(argv[1], "crc8") == 0) {
        len = parse_hex(argv[2], buf, sizeof buf);
        printf("%02x\n", crc8_maxim(buf, len));
    } else if (strcmp(argv[1], "invert") == 0) {
        len = parse_hex(argv[2], buf, sizeof buf);
        invert_bytes(buf, len);
        print_hex(buf, len);
        printf("\n");
    } else if (strcmp(argv[1], "pack") == 0) {
        unsigned short channels[TOTAL_CHANNELS];
        size_t n = 0;
        char *tok;
        char *work = dup_string(argv[2]);
        unsigned char packed[PACKED_SIZE];

        if (!work) return 1;
        for (tok = strtok(work, ","); tok && n < TOTAL_CHANNELS; tok = strtok(NULL, ",")) {
            channels[n++] = (unsigned short)(atoi(tok) & 0x07FF);
        }
        pack_channels(channels, n, packed, sizeof packed);
        print_hex(packed, sizeof packed);
        printf("\n");
        free(work);
    } else if (strcmp(argv[1], "frame") == 0) {
        unsigned char payload[512];
        unsigned char crc[2];
        size_t plen;

        plen = parse_hex(argv[2], payload, sizeof payload);
        if (plen > 255) {
            fprintf(stderr, "载荷过长\n");
            return 1;
        }
        printf("a55a%02x", (unsigned)plen);
        print_hex(payload, plen);
        /* CRC 覆盖 长度字段 + 载荷（不含帧头），与 Python 侧一致 */
        {
            unsigned char body[256];
            body[0] = (unsigned char)plen;
            memcpy(body + 1, payload, plen);
            crc16_modbus_bytes(body, plen + 1, crc);
            print_hex(crc, 2);
        }
        printf("\n");
    } else {
        return usage();
    }

    return 0;
}
