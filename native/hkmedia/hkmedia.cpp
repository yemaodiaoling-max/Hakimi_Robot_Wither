// hkmedia: prompt-side media reference compaction for Hakimi.
//
// The model never needs absolute cache paths, CDN signatures or tracking
// parameters.  It needs a short, stable handle that tools can resolve again.
// Every function here is pure (no I/O, no global state) and mirrored byte for
// byte by hakimi/core/media_codes.py, which is the fallback when this
// library is not built.  tests/test_media_codes.py checks the parity.
//
// C ABI (all strings are UTF-8, lengths in bytes, outputs NUL-terminated):
//   int    hk_version(void)
//   int    hk_image_code(path, path_len, n, out, cap)        -> code length
//   size_t hk_compact(text, len, table, table_len, flags, out, cap)
//   size_t hk_trim_quotes(text, len, ids, ids_len, out, cap)
// hk_compact / hk_trim_quotes return the full output size; when it is >= cap
// the caller retries with a larger buffer.
#include <cstdint>
#include <cstring>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace {

using std::string;
using std::string_view;

constexpr int kVersion = 1;
constexpr char kFirst[] = "abcdefghjkmnpqrstvwxyz";             // 22 letters
constexpr char kRest[] = "0123456789abcdefghjkmnpqrstvwxyz";    // Crockford base32

const string kImageMarkers[] = {"群友发图", "表情包图片"};
const string kMarkerTail = "(可用view_file看图):";

char lower(char c) { return (c >= 'A' && c <= 'Z') ? char(c - 'A' + 'a') : c; }

string normalize_path(string_view path) {
    string out(path);
    for (char &c : out) c = (c == '\\') ? '/' : lower(c);
    return out;
}

string_view basename(string_view path) {
    size_t cut = path.find_last_of("/\\");
    return cut == string_view::npos ? path : path.substr(cut + 1);
}

string image_code(string_view path, int n) {
    if (n < 3) n = 3;
    if (n > 12) n = 12;
    uint64_t h = 1469598103934665603ULL;
    for (char c : basename(path)) {
        h ^= uint8_t(lower(c));
        h *= 1099511628211ULL;
    }
    string out;
    out.push_back(kFirst[h % 22]);
    h /= 22;
    for (int i = 1; i < n; ++i) {
        out.push_back(kRest[h % 32]);
        h /= 32;
    }
    return out;
}

bool starts_with(string_view s, size_t at, string_view prefix) {
    return s.size() >= at && s.size() - at >= prefix.size() && s.compare(at, prefix.size(), prefix) == 0;
}

bool is_space(char c) { return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\f' || c == '\v'; }

string_view trim(string_view s) {
    while (!s.empty() && is_space(s.front())) s.remove_prefix(1);
    while (!s.empty() && is_space(s.back())) s.remove_suffix(1);
    return s;
}

// Truncate to at most `limit` code points without splitting a UTF-8 sequence.
string clip(string_view s, size_t limit) {
    size_t points = 0, i = 0;
    while (i < s.size()) {
        if (points == limit) return string(s.substr(0, i)) + "…";
        unsigned char c = s[i];
        size_t step = c < 0x80 ? 1 : (c >> 5) == 0x6 ? 2 : (c >> 4) == 0xE ? 3 : (c >> 3) == 0x1E ? 4 : 1;
        i += step;
        ++points;
    }
    return string(s);
}

void put_utf8(string &out, uint32_t cp) {
    if (cp < 0x80) out.push_back(char(cp));
    else if (cp < 0x800) { out.push_back(char(0xC0 | (cp >> 6))); out.push_back(char(0x80 | (cp & 0x3F))); }
    else if (cp < 0x10000) {
        out.push_back(char(0xE0 | (cp >> 12))); out.push_back(char(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(char(0x80 | (cp & 0x3F)));
    } else {
        out.push_back(char(0xF0 | (cp >> 18))); out.push_back(char(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back(char(0x80 | ((cp >> 6) & 0x3F))); out.push_back(char(0x80 | (cp & 0x3F)));
    }
}

int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    c = lower(c);
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

// OneBot CQ parameter escapes.
string cq_unescape(string_view s) {
    string out;
    for (size_t i = 0; i < s.size();) {
        if (starts_with(s, i, "&#44;")) { out.push_back(','); i += 5; }
        else if (starts_with(s, i, "&#91;")) { out.push_back('['); i += 5; }
        else if (starts_with(s, i, "&#93;")) { out.push_back(']'); i += 5; }
        else if (starts_with(s, i, "&amp;")) { out.push_back('&'); i += 5; }
        else out.push_back(s[i++]);
    }
    return out;
}

string percent_decode(string_view s) {
    string out;
    for (size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '%' && i + 2 < s.size()) {
            int a = hexval(s[i + 1]), b = hexval(s[i + 2]);
            if (a >= 0 && b >= 0) { out.push_back(char(a * 16 + b)); i += 2; continue; }
        }
        out.push_back(s[i]);
    }
    return out;
}

string xml_unescape(string_view s) {
    string out;
    for (size_t i = 0; i < s.size();) {
        if (starts_with(s, i, "&amp;")) { out.push_back('&'); i += 5; }
        else if (starts_with(s, i, "&lt;")) { out.push_back('<'); i += 4; }
        else if (starts_with(s, i, "&gt;")) { out.push_back('>'); i += 4; }
        else if (starts_with(s, i, "&quot;")) { out.push_back('"'); i += 6; }
        else if (starts_with(s, i, "&apos;")) { out.push_back('\''); i += 6; }
        else if (starts_with(s, i, "&#")) {
            size_t j = i + 2;
            bool hex = j < s.size() && (s[j] == 'x' || s[j] == 'X');
            if (hex) ++j;
            uint32_t cp = 0;
            size_t digits = 0;
            while (j < s.size() && digits < 7) {
                int v = hex ? hexval(s[j]) : (s[j] >= '0' && s[j] <= '9' ? s[j] - '0' : -1);
                if (v < 0) break;
                cp = cp * (hex ? 16 : 10) + uint32_t(v);
                ++j; ++digits;
            }
            if (digits && j < s.size() && s[j] == ';' && cp > 0 && cp <= 0x10FFFF && !(cp >= 0xD800 && cp <= 0xDFFF)) {
                put_utf8(out, cp);
                i = j + 1;
            } else out.push_back(s[i++]);
        } else out.push_back(s[i++]);
    }
    return out;
}

// Value of `key` in "k=v,k=v" CQ parameter text (raw, not unescaped).
bool cq_param(string_view params, string_view key, string_view &value) {
    size_t i = 0;
    while (i <= params.size()) {
        size_t end = params.find(',', i);
        if (end == string_view::npos) end = params.size();
        string_view item = params.substr(i, end - i);
        size_t eq = item.find('=');
        if (eq != string_view::npos && item.substr(0, eq) == key) {
            value = item.substr(eq + 1);
            return true;
        }
        i = end + 1;
    }
    return false;
}

bool url_char(unsigned char c) {
    if (c <= 0x20 || c >= 0x7F) return false;
    return !std::strchr("\"'<>[]{}|\\^`", c);
}

bool tracking_key(string_view key, string_view value) {
    string k;
    for (char c : key) k.push_back(lower(c));
    static const char *exact[] = {"spm_id_from", "spm", "vd_source", "from_spmid", "is_from_webapp",
                                  "sender_device", "unique_k", "wxshare_count", "share_source",
                                  "share_medium", "share_plat", "share_session_id", "share_from",
                                  "share_tag", "fbclid", "gclid"};
    for (const char *e : exact) if (k == e) return true;
    if (k.rfind("utm_", 0) == 0) return true;
    if (k == "share" && !value.empty()) {
        for (char c : value) if (c < '0' || c > '9') return false;
        return true;
    }
    if (k == "fr") return value.substr(0, 5) == "share";
    return false;
}

string host_of(string_view url) {
    size_t start = url.find("://");
    if (start == string_view::npos) return "";
    start += 3;
    size_t end = url.find_first_of("/?#", start);
    string host(url.substr(start, end == string_view::npos ? string_view::npos : end - start));
    size_t at = host.rfind('@');
    if (at != string::npos) host = host.substr(at + 1);
    size_t colon = host.find(':');
    if (colon != string::npos) host = host.substr(0, colon);
    for (char &c : host) c = lower(c);
    return host;
}

bool temp_media_host(const string &host) {
    return host == "multimedia.nt.qq.com.cn" || host == "gchat.qpic.cn" || host == "c2cpicdw.qpic.cn";
}

// Cleans one URL (already isolated).  Returns the replacement text.
string clean_url(string_view raw) {
    string url;
    for (size_t i = 0; i < raw.size();) {
        if (starts_with(raw, i, "&amp;")) { url.push_back('&'); i += 5; }
        else url.push_back(raw[i++]);
    }
    if (temp_media_host(host_of(url))) return "[QQ临时媒体链接]";
    size_t hash = url.find('#');
    string fragment = hash == string::npos ? "" : url.substr(hash);
    string head = hash == string::npos ? url : url.substr(0, hash);
    size_t q = head.find('?');
    if (q == string::npos) return url;
    string base = head.substr(0, q), query = head.substr(q + 1), kept;
    size_t i = 0;
    while (i <= query.size()) {
        size_t end = query.find('&', i);
        if (end == string::npos) end = query.size();
        string_view item = string_view(query).substr(i, end - i);
        size_t eq = item.find('=');
        string_view key = eq == string_view::npos ? item : item.substr(0, eq);
        string_view value = eq == string_view::npos ? string_view() : item.substr(eq + 1);
        if (!item.empty() && !tracking_key(key, value)) {
            if (!kept.empty()) kept.push_back('&');
            kept.append(item);
        }
        i = end + 1;
    }
    return base + (kept.empty() ? "" : "?" + kept) + fragment;
}

// Length of the URL starting at text[at] (which begins with http:// or https://).
size_t url_length(string_view text, size_t at) {
    size_t end = at;
    while (end < text.size() && url_char((unsigned char)text[end])) ++end;
    // Trailing sentence punctuation is prose, not URL.
    while (end > at) {
        char c = text[end - 1];
        if (std::strchr(".,;:!?", c)) { --end; continue; }
        if (c == ')') {
            string_view body = text.substr(at, end - at);
            size_t opens = 0, closes = 0;
            for (char d : body) { opens += d == '('; closes += d == ')'; }
            if (closes > opens) { --end; continue; }
        }
        break;
    }
    return end - at;
}

// First JSON string value of "key" in text, decoded.
bool json_string(string_view text, string_view key, string &value) {
    string needle = "\"" + string(key) + "\"";
    size_t at = 0;
    while ((at = text.find(needle, at)) != string_view::npos) {
        size_t i = at + needle.size();
        while (i < text.size() && is_space(text[i])) ++i;
        if (i >= text.size() || text[i] != ':') { at += needle.size(); continue; }
        ++i;
        while (i < text.size() && is_space(text[i])) ++i;
        if (i >= text.size() || text[i] != '"') { at += needle.size(); continue; }
        ++i;
        string out;
        while (i < text.size() && text[i] != '"') {
            char c = text[i];
            if (c != '\\') { out.push_back(c); ++i; continue; }
            if (i + 1 >= text.size()) break;
            char e = text[i + 1];
            i += 2;
            switch (e) {
                case 'n': out.push_back(' '); break;
                case 't': out.push_back(' '); break;
                case 'r': break;
                case 'b': case 'f': break;
                case 'u': {
                    if (i + 4 > text.size()) { i = text.size(); break; }
                    uint32_t cp = 0;
                    bool ok = true;
                    for (int k = 0; k < 4; ++k) { int v = hexval(text[i + k]); if (v < 0) ok = false; cp = cp * 16 + uint32_t(v < 0 ? 0 : v); }
                    if (!ok) break;
                    i += 4;
                    if (cp >= 0xD800 && cp <= 0xDBFF && i + 6 <= text.size() && text[i] == '\\' && text[i + 1] == 'u') {
                        uint32_t lo = 0;
                        bool ok2 = true;
                        for (int k = 0; k < 4; ++k) { int v = hexval(text[i + 2 + k]); if (v < 0) ok2 = false; lo = lo * 16 + uint32_t(v < 0 ? 0 : v); }
                        if (ok2 && lo >= 0xDC00 && lo <= 0xDFFF) { cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00); i += 6; }
                    }
                    if (cp >= 0xD800 && cp <= 0xDFFF) break;
                    put_utf8(out, cp);
                    break;
                }
                default: out.push_back(e);
            }
        }
        value = string(trim(out));
        return true;
    }
    return false;
}

// Attribute value attr="..." or <tag>...</tag> from XML text, decoded.
bool xml_value(string_view text, string_view name, bool attribute, string &value) {
    if (attribute) {
        string needle = string(name) + "=\"";
        size_t at = 0;
        while ((at = text.find(needle, at)) != string_view::npos) {
            if (at > 0 && !is_space(text[at - 1])) { at += needle.size(); continue; }
            size_t start = at + needle.size(), end = text.find('"', start);
            if (end == string_view::npos) return false;
            value = string(trim(xml_unescape(text.substr(start, end - start))));
            return true;
        }
        return false;
    }
    string open = "<" + string(name), close = "</" + string(name) + ">";
    size_t at = text.find(open);
    while (at != string_view::npos) {
        size_t gt = text.find('>', at);
        if (gt == string_view::npos) return false;
        char next = text.size() > at + open.size() ? text[at + open.size()] : '\0';
        if (next == '>' || is_space(next)) {
            size_t end = text.find(close, gt + 1);
            if (end == string_view::npos) return false;
            value = string(trim(xml_unescape(text.substr(gt + 1, end - gt - 1))));
            return true;
        }
        at = text.find(open, at + 1);
    }
    return false;
}

void add_part(std::vector<string> &parts, const string &raw) {
    string part = clip(trim(raw), 60);
    if (part.empty()) return;
    for (string &existing : parts) {
        if (existing.find(part) != string::npos) return;
        if (part.find(existing) != string::npos) { existing = part; return; }
    }
    parts.push_back(part);
}

string card(const std::vector<string> &parts, const string &url) {
    string out = "[卡片";
    for (size_t i = 0; i < parts.size(); ++i) out += (i ? "｜" : ":") + parts[i];
    if (!url.empty()) out += " 链接:" + clean_url(url);
    return out + "]";
}

bool hash_like(string_view stem) {
    if (stem.size() < 16) return false;
    for (char c : stem) if (hexval(c) < 0 && c != '-') return false;
    return true;
}

bool sticker_name(string_view name) {
    size_t dot = name.rfind('.');
    if (dot == string_view::npos || dot == 0 || dot > 100) return false;
    for (size_t i = 0; i < dot; ++i) {
        char c = name[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-')) return false;
    }
    string ext;
    for (char c : name.substr(dot + 1)) ext.push_back(lower(c));
    return ext == "gif" || ext == "png" || ext == "jpg" || ext == "jpeg" || ext == "webp";
}

// Authorized image path -> code.  Only an exact (normalized) path match maps to
// a code: chat text that merely imitates a marker never names a real image.
struct Table {
    std::unordered_map<string, string> full;
    explicit Table(string_view raw) {
        size_t i = 0;
        while (i < raw.size()) {
            size_t end = raw.find('\n', i);
            if (end == string_view::npos) end = raw.size();
            string_view line = raw.substr(i, end - i);
            size_t tab = line.find('\t');
            if (tab != string_view::npos) full.emplace(normalize_path(line.substr(0, tab)), string(line.substr(tab + 1)));
            i = end + 1;
        }
    }
    const string *find(string_view path) const {
        auto it = full.find(normalize_path(path));
        return it == full.end() ? nullptr : &it->second;
    }
};

string cq_image(string_view params, const Table &table) {
    string_view file;
    if (!cq_param(params, "file", file) || file.empty()) return "[图片]";
    string path = percent_decode(file);
    if (path.rfind("file:///", 0) == 0) path = path.substr(8);
    if (const string *code = table.find(path)) return "[图片#" + *code + "]";
    string normalized = path;
    for (char &c : normalized) if (c == '\\') c = '/';
    string name(basename(normalized));
    if (!sticker_name(name)) return "[图片]";
    string_view stem = string_view(name).substr(0, name.rfind('.'));
    if (hash_like(stem)) return "[表情#" + image_code(name, 4) + "]";
    return "[表情:" + name + "]";
}

// Returns true and sets `out` when the CQ code should be rewritten.
bool cq_rewrite(string_view type, string_view params, const Table &table, string &out) {
    string_view v;
    if (type == "image") { out = cq_image(params, table); return true; }
    if (type == "record") { out = "[语音]"; return true; }
    if (type == "video") { out = "[视频]"; return true; }
    if (type == "poke") { out = "[戳一戳]"; return true; }
    if (type == "file") {
        string name;
        if (cq_param(params, "name", v) || cq_param(params, "file", v)) name = string(basename(cq_unescape(v)));
        out = name.empty() ? "[文件]" : "[文件:" + clip(name, 60) + "]";
        return true;
    }
    if (type == "mface") {
        string summary;
        if (cq_param(params, "summary", v)) summary = string(trim(cq_unescape(v)));
        if (summary.size() >= 2 && summary.front() == '[' && summary.back() == ']') summary = summary.substr(1, summary.size() - 2);
        out = summary.empty() ? "[表情]" : "[表情:" + clip(summary, 30) + "]";
        return true;
    }
    if (type == "location") {
        string title;
        if (cq_param(params, "title", v)) title = string(trim(cq_unescape(v)));
        out = title.empty() ? "[位置]" : "[位置:" + clip(title, 60) + "]";
        return true;
    }
    if (type == "music") {
        string title;
        if (cq_param(params, "title", v)) title = string(trim(cq_unescape(v)));
        out = title.empty() ? "[音乐分享]" : "[音乐分享:" + clip(title, 60) + "]";
        return true;
    }
    if (type == "contact") {
        bool group = cq_param(params, "type", v) && v == "group";
        out = group ? "[推荐群聊]" : "[推荐联系人]";
        return true;
    }
    if (type == "share") {
        std::vector<string> parts;
        string url;
        if (cq_param(params, "title", v)) add_part(parts, cq_unescape(v));
        if (cq_param(params, "content", v)) add_part(parts, cq_unescape(v));
        if (cq_param(params, "url", v)) url = cq_unescape(v);
        out = card(parts, url);
        return true;
    }
    if (type == "json") {
        if (!cq_param(params, "data", v)) return false;
        string data = cq_unescape(v), value, url;
        std::vector<string> parts;
        for (const char *key : {"prompt", "title", "desc"}) if (json_string(data, key, value)) add_part(parts, value);
        for (const char *key : {"qqdocurl", "jumpUrl", "url"}) if (json_string(data, key, value) && starts_with(value, 0, "http")) { url = value; break; }
        out = card(parts, url);
        return true;
    }
    if (type == "xml") {
        if (!cq_param(params, "data", v)) return false;
        string data = cq_unescape(v), value, url;
        std::vector<string> parts;
        if (xml_value(data, "brief", true, value)) add_part(parts, value);
        if (xml_value(data, "title", false, value)) add_part(parts, value);
        if (xml_value(data, "summary", false, value)) add_part(parts, value);
        if (xml_value(data, "url", true, value) && starts_with(value, 0, "http")) url = value;
        out = card(parts, url);
        return true;
    }
    return false;
}

bool boundary_before_url(string_view text, size_t at) {
    if (at == 0) return true;
    unsigned char c = text[at - 1];
    return !((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '/' || c == '=' || c == '_' || c == '-' || c == '.');
}

string compact(string_view text, const Table &table, int flags) {
    const bool technical = flags & 1;
    string out;
    out.reserve(text.size());
    size_t i = 0;
    while (i < text.size()) {
        char c = text[i];
        if (c == '[') {
            bool handled = false;
            for (const string &label : kImageMarkers) {
                size_t head = 1 + label.size();
                if (!starts_with(text, i + 1, label) || !starts_with(text, i + head, kMarkerTail)) continue;
                size_t start = i + head + kMarkerTail.size();
                size_t end = start;
                while (end < text.size() && text[end] != ']' && text[end] != '\n') ++end;
                if (end >= text.size() || text[end] != ']') break;
                string_view path = trim(text.substr(start, end - start));
                if (path.empty()) break;
                const string *code = table.find(path);
                out += "[" + label + (code ? "#" + *code : string(":无可读缓存")) + "]";
                i = end + 1;
                handled = true;
                break;
            }
            if (handled) continue;
            if (!technical && starts_with(text, i, "[CQ:")) {
                size_t end = text.find(']', i);
                if (end != string_view::npos) {
                    string_view body = text.substr(i + 4, end - i - 4);
                    size_t comma = body.find(',');
                    string_view type = body.substr(0, comma);
                    string_view params = comma == string_view::npos ? string_view() : body.substr(comma + 1);
                    string replacement;
                    if (cq_rewrite(type, params, table, replacement)) {
                        out += replacement;
                        i = end + 1;
                        continue;
                    }
                }
            }
        } else if (!technical && (c == 'h' || c == 'H') && (starts_with(text, i, "http://") || starts_with(text, i, "https://")) &&
                   boundary_before_url(text, i)) {
            size_t n = url_length(text, i);
            if (n > 8) {
                out += clean_url(text.substr(i, n));
                i += n;
                continue;
            }
        }
        out.push_back(c);
        ++i;
    }
    return out;
}

string trim_quotes(string_view text, const std::unordered_set<string> &ids) {
    static const string open = "[引用 ", middle = " 的消息(id=";
    string out;
    out.reserve(text.size());
    size_t i = 0;
    while (i < text.size()) {
        if (starts_with(text, i, open)) {
            size_t nick_start = i + open.size();
            size_t mid = text.find(middle, nick_start);
            if (mid != string_view::npos && mid - nick_start <= 120 && text.substr(nick_start, mid - nick_start).find('\n') == string_view::npos) {
                size_t id_start = mid + middle.size(), j = id_start;
                if (j < text.size() && text[j] == '-') ++j;
                while (j < text.size() && text[j] >= '0' && text[j] <= '9') ++j;
                string id(text.substr(id_start, j - id_start));
                bool digits = j > id_start && !(id == "-");
                if (digits && starts_with(text, j, "): \"") && ids.count(id)) {
                    size_t body_end = text.find("\"]", j + 4);
                    if (body_end != string_view::npos) {
                        out += string(text.substr(i, j - i)) + ")]";
                        i = body_end + 2;
                        continue;
                    }
                }
            }
        }
        out.push_back(text[i]);
        ++i;
    }
    return out;
}

size_t emit(const string &result, char *out, size_t cap) {
    if (out && cap) {
        size_t n = result.size() < cap ? result.size() : cap - 1;
        std::memcpy(out, result.data(), n);
        out[n] = '\0';
    }
    return result.size();
}

}  // namespace

extern "C" {

int hk_version(void) { return kVersion; }

int hk_image_code(const char *path, size_t path_len, int n, char *out, size_t cap) {
    if (!path) return -1;
    return int(emit(image_code(string_view(path, path_len), n), out, cap));
}

size_t hk_compact(const char *text, size_t len, const char *table, size_t table_len, int flags, char *out, size_t cap) {
    if (!text) return emit("", out, cap);
    Table parsed(table ? string_view(table, table_len) : string_view());
    return emit(compact(string_view(text, len), parsed, flags), out, cap);
}

size_t hk_trim_quotes(const char *text, size_t len, const char *ids, size_t ids_len, char *out, size_t cap) {
    if (!text) return emit("", out, cap);
    std::unordered_set<string> set;
    string_view raw = ids ? string_view(ids, ids_len) : string_view();
    size_t i = 0;
    while (i < raw.size()) {
        size_t end = raw.find('\n', i);
        if (end == string_view::npos) end = raw.size();
        if (end > i) set.emplace(raw.substr(i, end - i));
        i = end + 1;
    }
    return emit(trim_quotes(string_view(text, len), set), out, cap);
}

}  // extern "C"
