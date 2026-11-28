// Operator CLI for libhkmedia: inspect what the model will see.
//   hkmedia code PATH...                 print the stable code of each image
//   hkmedia compact [TABLE] < text       rewrite media references (TABLE: lines "path<TAB>code")
//   hkmedia quotes ID[,ID...] < text     drop quote bodies of visible message ids
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <sstream>
#include <string>
#include <vector>

extern "C" {
int hk_version(void);
int hk_image_code(const char *path, size_t path_len, int n, char *out, size_t cap);
size_t hk_compact(const char *text, size_t len, const char *table, size_t table_len, int flags, char *out, size_t cap);
size_t hk_trim_quotes(const char *text, size_t len, const char *ids, size_t ids_len, char *out, size_t cap);
}

namespace {

std::string read_all(std::istream &in) {
    return std::string(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
}

template <typename Fn>
std::string run(Fn fn) {
    std::vector<char> buffer(4096);
    for (;;) {
        size_t needed = fn(buffer.data(), buffer.size());
        if (needed < buffer.size()) return std::string(buffer.data(), needed);
        buffer.resize(needed + 1);
    }
}

int usage() {
    std::fprintf(stderr, "usage: hkmedia code PATH... | compact [TABLE] < text | quotes IDS < text\n");
    return 2;
}

}  // namespace

int main(int argc, char **argv) {
    if (argc < 2) return usage();
    std::string command = argv[1];
    if (command == "version") {
        std::printf("%d\n", hk_version());
        return 0;
    }
    if (command == "code") {
        for (int i = 2; i < argc; ++i) {
            char out[16];
            hk_image_code(argv[i], std::strlen(argv[i]), 4, out, sizeof out);
            std::printf("%s\t%s\n", out, argv[i]);
        }
        return argc > 2 ? 0 : usage();
    }
    if (command == "compact") {
        std::string table;
        if (argc > 2) {
            std::ifstream file(argv[2], std::ios::binary);
            if (!file) { std::fprintf(stderr, "cannot read %s\n", argv[2]); return 1; }
            table = read_all(file);
        }
        std::string text = read_all(std::cin);
        std::cout << run([&](char *out, size_t cap) {
            return hk_compact(text.data(), text.size(), table.data(), table.size(), 0, out, cap);
        });
        return 0;
    }
    if (command == "quotes" && argc > 2) {
        std::string ids = argv[2];
        for (char &c : ids) if (c == ',') c = '\n';
        std::string text = read_all(std::cin);
        std::cout << run([&](char *out, size_t cap) {
            return hk_trim_quotes(text.data(), text.size(), ids.data(), ids.size(), out, cap);
        });
        return 0;
    }
    return usage();
}
