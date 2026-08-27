#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_PORTS 64
#define MAX_TOKENS 128
#define MAX_NAME 96

typedef struct {
    char name[MAX_NAME];
    char dir[8];
    int width;
} Port;

static char *xstrdup(const char *s) {
    size_t n = strlen(s ? s : "");
    char *out = (char *)malloc(n + 1);
    if (!out) return NULL;
    memcpy(out, s ? s : "", n);
    out[n] = 0;
    return out;
}

static void safe_copy(char *dst, size_t dst_n, const char *src) {
    if (!dst || dst_n == 0) return;
    if (!src) src = "";
    snprintf(dst, dst_n, "%s", src);
}

static void upper_copy(char *dst, const char *src, size_t maxn) {
    size_t j = 0;
    if (!dst || maxn == 0) return;
    for (size_t i = 0; src && src[i] && j + 1 < maxn; i++) {
        unsigned char c = (unsigned char)src[i];
        dst[j++] = (char)toupper(c);
    }
    dst[j] = 0;
}


static int equals_ci(const char *a, const char *b) {
    while (*a && *b) {
        if (toupper((unsigned char)*a) != toupper((unsigned char)*b)) return 0;
        a++;
        b++;
    }
    return *a == 0 && *b == 0;
}

static int is_ident_start(char c) {
    return isalpha((unsigned char)c) || c == '_' || c == '$';
}

static int is_ident_char(char c) {
    return isalnum((unsigned char)c) || c == '_' || c == '$';
}

static int keyword_at_ci(const char *s, size_t pos, const char *keyword) {
    size_t n = strlen(keyword);
    if (pos > 0 && is_ident_char(s[pos - 1])) return 0;
    for (size_t i = 0; i < n; i++) {
        if (!s[pos + i]) return 0;
        if (toupper((unsigned char)s[pos + i]) != toupper((unsigned char)keyword[i])) return 0;
    }
    return !is_ident_char(s[pos + n]);
}

static char *read_file(const char *path) {
    FILE *f = fopen(path, "rb");
    long n;
    size_t got;
    char *buf;
    if (!f) return (char *)calloc(1, 1);
    if (fseek(f, 0, SEEK_END) != 0) {
        fclose(f);
        return (char *)calloc(1, 1);
    }
    n = ftell(f);
    if (n < 0) {
        fclose(f);
        return (char *)calloc(1, 1);
    }
    rewind(f);
    buf = (char *)calloc((size_t)n + 1, 1);
    if (!buf) {
        fclose(f);
        return (char *)calloc(1, 1);
    }
    got = fread(buf, 1, (size_t)n, f);
    buf[got] = 0;
    fclose(f);
    return buf;
}

static char *strip_comments(const char *s) {
    size_t n = strlen(s ? s : "");
    char *out = (char *)calloc(n + 1, 1);
    size_t j = 0;
    int line = 0;
    int block = 0;
    int str = 0;
    if (!out) return (char *)calloc(1, 1);

    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        char nx = (i + 1 < n) ? s[i + 1] : 0;
        if (line) {
            if (c == '\n') {
                line = 0;
                out[j++] = '\n';
            }
            continue;
        }
        if (block) {
            if (c == '*' && nx == '/') {
                block = 0;
                i++;
            }
            continue;
        }
        if (!str && c == '/' && nx == '/') {
            line = 1;
            i++;
            continue;
        }
        if (!str && c == '/' && nx == '*') {
            block = 1;
            i++;
            continue;
        }
        if (c == '"') str = !str;
        out[j++] = c;
    }
    out[j] = 0;
    return out;
}

static void normalize_name(const char *src, char *dst, size_t maxn) {
    char tmp[MAX_NAME];
    size_t j = 0;
    if (!dst || maxn == 0) return;
    dst[0] = 0;
    if (!src) return;

    while (*src && (*src == '"' || *src == '\'' || isspace((unsigned char)*src) || *src == '{')) src++;
    for (size_t i = 0; src[i] && j + 1 < sizeof(tmp); i++) {
        char c = src[i];
        if (c == '[' || c == '(' || c == '=' || c == ',' || c == ';' ||
            c == '}' || c == ')' || isspace((unsigned char)c)) {
            break;
        }
        tmp[j++] = c;
    }
    tmp[j] = 0;
    upper_copy(dst, tmp, maxn);
}

static int width_from_decl(const char *text) {
    const char *lb = strchr(text, '[');
    const char *colon = lb ? strchr(lb, ':') : NULL;
    const char *rb = colon ? strchr(colon, ']') : NULL;
    long a;
    long b;
    char left[24];
    char right[24];
    size_t n1;
    size_t n2;

    if (!lb || !colon || !rb) return 1;
    n1 = (size_t)(colon - lb - 1);
    n2 = (size_t)(rb - colon - 1);
    if (n1 == 0 || n2 == 0 || n1 >= sizeof(left) || n2 >= sizeof(right)) return 1;

    memcpy(left, lb + 1, n1);
    left[n1] = 0;
    memcpy(right, colon + 1, n2);
    right[n2] = 0;

    a = strtol(left, NULL, 10);
    b = strtol(right, NULL, 10);
    if (a == 0 && left[0] != '0' && left[0] != '-') return 1;
    if (b == 0 && right[0] != '0' && right[0] != '-') return 1;
    return (int)(labs(a - b) + 1);
}

static int has_token(const char arr[][MAX_NAME], int count, const char *tok) {
    for (int i = 0; i < count; i++) {
        if (strcmp(arr[i], tok) == 0) return 1;
    }
    return 0;
}

static void add_token(char arr[][MAX_NAME], int *count, const char *tok, int max_count) {
    char norm[MAX_NAME];
    normalize_name(tok, norm, sizeof(norm));
    if (!norm[0] || *count >= max_count || has_token(arr, *count, norm)) return;
    safe_copy(arr[*count], MAX_NAME, norm);
    (*count)++;
}

static void add_port(Port *ports, int *count, const char *name, const char *dir, int width) {
    char norm[MAX_NAME];
    normalize_name(name, norm, sizeof(norm));
    if (!norm[0] || *count >= MAX_PORTS) return;

    for (int i = 0; i < *count; i++) {
        if (strcmp(ports[i].name, norm) == 0 && strcmp(ports[i].dir, dir) == 0) {
            if (width > ports[i].width) ports[i].width = width;
            return;
        }
    }

    safe_copy(ports[*count].name, sizeof(ports[*count].name), norm);
    safe_copy(ports[*count].dir, sizeof(ports[*count].dir), dir);
    ports[*count].width = width > 0 ? width : 1;
    (*count)++;
}

static int reserved_word(const char *name) {
    static const char *WORDS[] = {
        "INPUT", "OUTPUT", "INOUT", "WIRE", "REG", "LOGIC", "SIGNED", "UNSIGNED",
        "MODULE", "BEGIN", "END", "IF", "ELSE", "CASE", "ENDCASE", "ALWAYS",
        "ASSIGN", "PARAMETER", "LOCALPARAM", "GENERATE", "ENDGENERATE", NULL
    };
    for (int i = 0; WORDS[i]; i++) {
        if (equals_ci(name, WORDS[i])) return 1;
    }
    return 0;
}

static void scan_verilog_ports(
    const char *text,
    Port *ports,
    int *port_count,
    char tokens[][MAX_NAME],
    int *token_count
) {
    size_t n = strlen(text ? text : "");
    size_t i = 0;

    while (i < n) {
        const char *dir = NULL;
        size_t keyword_len = 0;

        if (keyword_at_ci(text, i, "input")) {
            dir = "input";
            keyword_len = 5;
        } else if (keyword_at_ci(text, i, "output")) {
            dir = "output";
            keyword_len = 6;
        } else if (keyword_at_ci(text, i, "inout")) {
            dir = "inout";
            keyword_len = 5;
        }

        if (!dir) {
            i++;
            continue;
        }

        size_t segment_start = i + keyword_len;
        size_t segment_end = segment_start;
        int bracket_depth = 0;
        while (segment_end < n) {
            char ch = text[segment_end];
            if (ch == '[') bracket_depth++;
            else if (ch == ']' && bracket_depth > 0) bracket_depth--;

            if (bracket_depth == 0) {
                if (ch == ';' || ch == ')') break;
                if (
                    keyword_at_ci(text, segment_end, "input") ||
                    keyword_at_ci(text, segment_end, "output") ||
                    keyword_at_ci(text, segment_end, "inout")
                ) {
                    break;
                }
            }
            segment_end++;
        }

        size_t segment_len = segment_end - segment_start;
        char *segment = (char *)calloc(segment_len + 1, 1);
        if (!segment) return;
        memcpy(segment, text + segment_start, segment_len);
        segment[segment_len] = 0;
        int width = width_from_decl(segment);

        size_t p = 0;
        bracket_depth = 0;
        while (p < segment_len) {
            char ch = segment[p];
            if (ch == '[') {
                bracket_depth++;
                p++;
                continue;
            }
            if (ch == ']') {
                if (bracket_depth > 0) bracket_depth--;
                p++;
                continue;
            }
            if (bracket_depth > 0 || !is_ident_start(ch)) {
                p++;
                continue;
            }

            char ident[MAX_NAME];
            size_t j = 0;
            while (p < segment_len && is_ident_char(segment[p]) && j + 1 < sizeof(ident)) {
                ident[j++] = segment[p++];
            }
            ident[j] = 0;

            if (!ident[0] || reserved_word(ident)) continue;

            /* Ignore parameter/type words commonly found in declarations. */
            if (
                equals_ci(ident, "integer") ||
                equals_ci(ident, "bit") ||
                equals_ci(ident, "tri") ||
                equals_ci(ident, "var")
            ) {
                continue;
            }

            add_port(ports, port_count, ident, dir, width);
            add_token(tokens, token_count, ident, MAX_TOKENS);
            if (*port_count >= MAX_PORTS || *token_count >= MAX_TOKENS) break;
        }

        free(segment);
        if (*port_count >= MAX_PORTS || *token_count >= MAX_TOKENS) break;
        i = segment_end > i ? segment_end : i + keyword_len;
    }
}

static void copy_last_quoted_value(const char *line, char *dst, size_t dst_n) {
    const char *last = strrchr(line, '"');
    const char *first = NULL;
    if (!last) return;

    for (const char *p = last; p > line; p--) {
        if (*(p - 1) == '"') {
            first = p - 1;
            break;
        }
    }
    if (!first || last <= first + 1) return;

    size_t n = (size_t)(last - first - 1);
    if (n >= dst_n) n = dst_n - 1;
    memcpy(dst, first + 1, n);
    dst[n] = 0;
}

static void scan_qsf(
    const char *text,
    char *family,
    size_t family_n,
    char *device,
    size_t device_n,
    char *board,
    size_t board_n,
    char targets[][MAX_NAME],
    int *target_count
) {
    char *copy = xstrdup(text ? text : "");
    char *line = NULL;
    if (!copy) return;

    for (line = strtok(copy, "\n"); line; line = strtok(NULL, "\n")) {
        char upper[2048];
        upper_copy(upper, line, sizeof(upper));

        if (!family[0] && strstr(upper, "-NAME FAMILY")) {
            copy_last_quoted_value(line, family, family_n);
            if (!family[0]) {
                char *p = strstr(upper, "FAMILY");
                if (p) {
                    size_t off = (size_t)(p - upper) + strlen("FAMILY");
                    while (line[off] && isspace((unsigned char)line[off])) off++;
                    normalize_name(line + off, family, family_n);
                }
            }
        }

        if (!device[0] && strstr(upper, "-NAME DEVICE")) {
            char *p = strstr(upper, "DEVICE");
            if (p) {
                size_t off = (size_t)(p - upper) + strlen("DEVICE");
                while (line[off] && isspace((unsigned char)line[off])) off++;
                normalize_name(line + off, device, device_n);
            }
        }

        if (!board[0] && strstr(upper, "-NAME BOARD")) {
            copy_last_quoted_value(line, board, board_n);
            if (!board[0]) {
                char *p = strstr(upper, "BOARD");
                if (p) {
                    size_t off = (size_t)(p - upper) + strlen("BOARD");
                    while (line[off] && isspace((unsigned char)line[off])) off++;
                    normalize_name(line + off, board, board_n);
                }
            }
        }

        {
            char *to = strstr(upper, "-TO");
            if (to) {
                size_t off = (size_t)(to - upper) + 3;
                while (line[off] && isspace((unsigned char)line[off])) off++;

                size_t p = off;
                while (line[p] && *target_count < MAX_TOKENS) {
                    while (
                        line[p] &&
                        !is_ident_start(line[p]) &&
                        line[p] != '\\'
                    ) {
                        p++;
                    }
                    if (!line[p]) break;

                    char target[MAX_NAME];
                    size_t j = 0;
                    if (line[p] == '\\') p++;
                    while (
                        line[p] &&
                        !isspace((unsigned char)line[p]) &&
                        line[p] != ',' &&
                        line[p] != '}' &&
                        line[p] != '"'
                    ) {
                        if (j + 1 < sizeof(target)) target[j++] = line[p];
                        p++;
                    }
                    target[j] = 0;
                    if (target[0]) add_token(targets, target_count, target, MAX_TOKENS);
                }
            }
        }
    }
    free(copy);
}

static void print_json_string(const char *s) {
    putchar('"');
    for (size_t i = 0; s && s[i]; i++) {
        char c = s[i];
        if (c == '"' || c == '\\') putchar('\\');
        if (c == '\n') {
            fputs("\\n", stdout);
            continue;
        }
        putchar(c);
    }
    putchar('"');
}

int main(int argc, char **argv) {
    const char *vpath = argc > 1 ? argv[1] : "";
    const char *qpath = argc > 2 ? argv[2] : "";
    char *raw_v = read_file(vpath);
    char *raw_q = read_file(qpath);
    char *v = strip_comments(raw_v);
    char *q = strip_comments(raw_q);

    Port ports[MAX_PORTS];
    int port_count = 0;
    char tokens[MAX_TOKENS][MAX_NAME] = {{0}};
    int token_count = 0;
    char qsf_targets[MAX_TOKENS][MAX_NAME] = {{0}};
    int qsf_target_count = 0;
    char family[128] = "";
    char device[128] = "";
    char board[128] = "";

    memset(ports, 0, sizeof(ports));
    scan_verilog_ports(v, ports, &port_count, tokens, &token_count);
    scan_qsf(
        q,
        family,
        sizeof(family),
        device,
        sizeof(device),
        board,
        sizeof(board),
        qsf_targets,
        &qsf_target_count
    );

    printf("{");
    printf("\"extractor_engine\":\"c_signal_evidence_only\",");

    printf("\"verilog_ports\":[");
    for (int i = 0; i < port_count; i++) {
        if (i) printf(",");
        printf("{\"name\":");
        print_json_string(ports[i].name);
        printf(",\"dir\":");
        print_json_string(ports[i].dir);
        printf(",\"width\":%d}", ports[i].width > 0 ? ports[i].width : 1);
    }
    printf("],");

    printf("\"verilog_signals\":[");
    for (int i = 0; i < token_count; i++) {
        if (i) printf(",");
        print_json_string(tokens[i]);
    }
    printf("],");

    printf("\"signal_widths\":{");
    for (int i = 0; i < port_count; i++) {
        if (i) printf(",");
        print_json_string(ports[i].name);
        printf(":%d", ports[i].width > 0 ? ports[i].width : 1);
    }
    printf("},");

    printf("\"qsf_family\":");
    print_json_string(family);
    printf(",\"qsf_device\":");
    print_json_string(device);
    printf(",\"qsf_board\":");
    print_json_string(board);

    printf(",\"qsf_targets\":[");
    for (int i = 0; i < qsf_target_count; i++) {
        if (i) printf(",");
        print_json_string(qsf_targets[i]);
    }
    printf("]}");

    free(raw_v);
    free(raw_q);
    free(v);
    free(q);
    return 0;
}
