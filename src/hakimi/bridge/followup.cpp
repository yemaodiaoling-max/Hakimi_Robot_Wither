#include <cstddef>
#include <string>
#include <string_view>
#include <initializer_list>

#ifdef _WIN32
#define HAKIMI_EXPORT __declspec(dllexport)
#else
#define HAKIMI_EXPORT __attribute__((visibility("default")))
#endif

namespace {
using View = std::string_view;
using Words = std::initializer_list<View>;
constexpr std::size_t max_bytes = 16384;

bool has(View value, Words words) {
    for (View word : words)
        if (value.find(word) != View::npos) return true;
    return false;
}

bool starts(View value, Words words) {
    for (View word : words)
        if (value.substr(0, word.size()) == word) return true;
    return false;
}

bool ends(View value, Words words) {
    for (View word : words)
        if (value.size() >= word.size() && value.substr(value.size() - word.size()) == word)
            return true;
    return false;
}

bool is(View value, Words words) {
    for (View word : words)
        if (value == word) return true;
    return false;
}

std::string normalize(View input) {
    std::string out;
    out.reserve(input.size());
    for (std::size_t p = 0; p < input.size(); ++p) {
        const unsigned char c = input[p];
        if (input.substr(p, 3) == "　") {
            p += 2;
        } else if (c > 32 && c != 127) {
            out.push_back(c >= 'A' && c <= 'Z' ? c + ('a' - 'A') : c);
            continue;
        }
        if (!out.empty() && out.back() != ' ') out.push_back(' ');
    }
    while (!out.empty() && out.back() == ' ') out.pop_back();
    return out;
}

View trim_marks(View value) {
    for (View mark : {"，", "。", "！", "？", "：", ",", ".", "!", "?", ":", " "}) {
        while (value.substr(0, mark.size()) == mark) value.remove_prefix(mark.size());
        while (value.size() >= mark.size() &&
               value.substr(value.size() - mark.size()) == mark) value.remove_suffix(mark.size());
    }
    return value;
}

bool is_media(View value) {
    return is(value, {"[图片]", "[语音]", "[视频]", "[文件]", "[表情]", "[动画表情]",
                      "[image]", "[video]", "[voice]", "[file]", "[sticker]"}) ||
           starts(value, {"[cq:image", "[cq:video", "[cq:record", "[cq:file"});
}

enum class Parameter { none, time, quantity, language, format };

bool budget_ceiling(View value) {
    bool ceiling = false;
    for (View prefix : {"最多", "不超过", "别超过", "上限", "at most ", "up to "}) {
        if (starts(value, {prefix})) {
            value.remove_prefix(prefix.size());
            ceiling = true;
            break;
        }
    }
    bool amount = false;
    while (!value.empty()) {
        if (value.front() >= '0' && value.front() <= '9') {
            value.remove_prefix(1);
        } else if (starts(value, {"一", "二", "两", "三", "四", "五", "六", "七", "八", "九",
                                  "零", "十", "百", "千", "万", "〇"})) {
            value.remove_prefix(3);
        } else {
            break;
        }
        amount = true;
    }
    return amount && (is(value, {"元以内", "块以内", "以内", "以下"}) ||
                      (ceiling && is(value, {"", "元", "块", "块钱"})));
}

bool bare_weather_location(View value) {
    if (value.size() < 9 || value.size() > 15 || value.size() % 3 != 0 ||
        value.substr(value.size() - 3) != "的") return false;
    value.remove_suffix(3);
    if (has(value, {"前", "后", "今", "昨", "明天", "刚", "那", "这", "哪", "什么", "左右", "周末",
                    "附近", "同样", "之前", "红色", "蓝色", "白色", "黑色", "免费", "便宜",
                    "真的", "假的", "好看", "简单", "英文", "中文", "要买", "不想", "较大"}))
        return false;
    // Two to four basic Han characters are a common city-name fragment in chat.
    while (!value.empty()) {
        const auto first = static_cast<unsigned char>(value[0]);
        const auto second = static_cast<unsigned char>(value[1]);
        const auto third = static_cast<unsigned char>(value[2]);
        if ((second & 0xc0) != 0x80 || (third & 0xc0) != 0x80) return false;
        const auto point = ((first & 0x0f) << 12) | ((second & 0x3f) << 6) | (third & 0x3f);
        if ((first & 0xf0) != 0xe0 || point < 0x4e00 || point > 0x9fff) return false;
        value.remove_prefix(3);
    }
    return true;
}

Parameter parameter(View value) {
    value = trim_marks(value);
    if (starts(value, {"就"})) value.remove_prefix(3);
    if (starts(value, {"定在"})) value.remove_prefix(6);
    if (is(value, {"今天", "明天", "后天", "今晚", "今早", "明早", "明晚", "周末",
                   "上午", "下午", "晚上", "下周", "这周", "周一", "周二", "周三",
                   "周四", "周五", "周六", "周日", "today", "tomorrow", "tonight",
                   "next week", "this weekend", "monday", "tuesday", "wednesday",
                   "thursday", "friday", "saturday", "sunday"})) return Parameter::time;
    for (View day : {"今天", "明天", "后天", "周一", "周二", "周三", "周四", "周五", "周六", "周日"}) {
        if (starts(value, {day}) &&
            is(value.substr(day.size()), {"早上", "上午", "中午", "下午", "晚上"}))
            return Parameter::time;
    }
    if (is(value, {"中文", "英文", "日文", "中文就行", "中文即可", "英文就行", "英文即可",
                   "english", "chinese", "japanese", "in english", "in chinese", "in japanese"}))
        return Parameter::language;
    if (is(value, {"pdf", "docx", "xlsx", "csv", "json", "markdown", "纯文本", "表格",
                   "as a table", "plain text"})) return Parameter::format;

    for (View prefix : {"今天", "明天", "后天", "下午", "上午", "晚上", "早上",
                        "today ", "tomorrow ", "at "})
        if (starts(value, {prefix})) value.remove_prefix(prefix.size());
    auto digits = [](View &tail) {
        const auto size = tail.size();
        while (!tail.empty() && tail.front() >= '0' && tail.front() <= '9')
            tail.remove_prefix(1);
        return size - tail.size();
    };
    if (!digits(value)) return Parameter::none;
    if (starts(value, {":", "："})) {
        value.remove_prefix(value.front() == ':' ? 1 : 3);
        if (digits(value) != 2) return Parameter::none;
        return is(value, {"", " am", " pm", "am", "pm"}) ? Parameter::time : Parameter::none;
    }
    if (starts(value, {"点"})) {
        value.remove_prefix(3);
        if (is(value, {"", "半", "整"})) return Parameter::time;
        return digits(value) && is(value, {"", "分"}) ? Parameter::time : Parameter::none;
    }
    if (is(value, {" am", " pm", "am", "pm", "号", "日", "月"})) return Parameter::time;
    if (is(value, {"个", "家", "条", "项", "份", "页", "字", "人", "元", "分钟", "小时",
                   "秒", "%", "％", " words", " pages", " items", " minutes", " hours"}))
        return Parameter::quantity;
    return Parameter::none;
}

bool relation(View previous, View next) {
    if (previous.empty() || next.empty() || is_media(previous) || is_media(next) ||
        previous == next) return false;
    if (is(trim_marks(previous), {"好", "好的", "嗯", "收到", "谢谢", "在吗", "你好",
                                  "ok", "okay", "thanks", "hello", "hi"}) ||
        is(trim_marks(next), {"好", "好的", "嗯", "收到", "谢谢", "在吗", "你好",
                              "ok", "okay", "thanks", "hello", "hi"})) return false;

    const bool correction = starts(next, {
        "说错了", "打错了", "写错了", "更正", "我说的是", "我的意思是", "是说",
        "不是要", "不是说", "不是这个", "不是那个", "不对，", "不对,", "不对 ", "不对是",
        "我刚才说错", "刚说错", "刚才说错", "刚刚说错", "改成", "改为", "换成", "应该是", "i meant ",
        "i mean ", "correction:", "sorry, i meant ", "instead of "}) ||
        (starts(next, {"不是"}) && has(next, {"而是", "，是", ",是"}));
    if (correction && next.size() <= 500) return true;

    const bool new_topic = starts(next, {
        "另一件事", "换个话题", "换个问题", "问个别的", "另外一个问题", "顺便问",
        "顺便帮", "顺便查", "顺便说", "再问个", "还有个问题", "还有一个问题",
        "另外问", "另外帮", "另外查", "单独", "先不说", "另一个问题", "新问题",
        "unrelated", "new question", "another question", "by the way", "separately"});
    if (new_topic) return false;
    const bool explicit_reference = starts(next, {
        "刚才那个", "刚才的", "刚刚那个", "刚刚的", "关于刚才", "关于刚刚", "关于上面",
        "上面那个", "上面那条", "上一条", "上条", "前面那个", "前面说的", "接着刚才",
        "补充一下", "补充一点", "再补充", "再具体一点", "再详细一点", "继续讲",
        "那如果", "那要是", "那刚才", "那这个呢", "那那个呢", "那这个怎么", "那它",
        "about the previous ", "about the above", "for the previous ", "in that case",
        "also, for that ", "to clarify", "to add to that", "one clarification"});
    if (explicit_reference && next.size() <= 700) return true;

    const auto short_next = trim_marks(next);
    const bool drawing_request = starts(previous, {"画", "绘制", "draw ", "paint "}) ||
        has(previous, {"帮我画", "帮忙画", "给我画", "请画", "想画一张", "生成一张图",
                       "生成图片", "画一幅", "draw me ", "please draw ", "generate an image"});
    if (drawing_request && short_next.size() <= 160 &&
        !has(short_next, {"怎么", "为什么", "如何", "能不能", "可不可以", "what ", "how ", "why "})) {
        const bool cancel_drawing = starts(short_next, {"不要画了", "不用画了", "别画了",
            "不画了", "取消绘图", "取消画图", "don't draw", "no image", "cancel the drawing"}) ||
            (starts(short_next, {"算了", "先算了"}) &&
             has(short_next, {"别画", "不画", "不用画", "不要画", "不用出图", "不要出图"}));
        if (cancel_drawing || starts(short_next, {"背景用", "背景改成", "背景要", "背景颜色",
            "画面风格", "风格用", "background in ", "background should be ", "use a watercolor style"}) ||
            (short_next.size() <= 48 && ends(short_next, {"风格", "watercolor style"}) &&
             !has(short_next, {"？", "?", "是什么", "帮我", "帮忙"}))) return true;
    }
    const auto kind = parameter(next);
    if (kind == Parameter::time)
        return has(previous, {"时间", "时候", "日期", "几点", "哪天", "预约", "提醒", "定时",
                              "安排", "日程", "会议", "航班", "出发", "天气", "见面", "约", "明天",
                              "今天", "后天", "schedule", "remind", "meeting", "appointment",
                              "weather", "date", "time", "flight", "today", "tomorrow"});
    if (kind == Parameter::quantity)
        return has(previous, {"几", "多少", "推荐", "列", "写", "限制", "篇幅", "预算",
                              "人数", "时长", "how many", "how much", "list", "write",
                              "suggest", "length", "budget", "limit", "recommend"});
    if (short_next.size() <= 160 &&
        !has(short_next, {"？", "?", "怎么", "为什么", "能不能", "可不可以", "帮我",
                          "how ", "why ", "can you ", "could you "})) {
        const bool budget = starts(short_next, {"人均", "预算", "每人", "budget is ", "per person "}) &&
            has(short_next, {"元", "块", "以内", "以下", "左右", "最高", "$", "¥", "under ",
                             "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "十", "百", "千", "万"});
        if (budget && has(previous, {"推荐", "找", "挑", "选", "买", "报价", "费用",
                                    "价格", "预算", "recommend", "find", "price", "budget"}))
            return true;
        if (budget_ceiling(short_next) &&
            has(previous, {"预算", "价格", "费用", "报价", "多少钱", "买", "选电脑", "选手机",
                           "采购", "cost", "price", "budget", "buy", "purchase"})) return true;
        const bool environment = starts(short_next, {"我用的是", "我的系统是", "系统是",
            "系统版本", "版本是", "环境是", "我这边是", "i'm using ", "i am using ",
            "my os is ", "running on "});
        if (environment && has(previous, {"报错", "错误", "日志", "崩溃", "闪退", "装不上",
            "打不开", "启动不了", "调试", "error", "traceback", "crash", "debug",
            "won't run", "won't start"})) return true;
        if (starts(short_next, {"带着", "带上", "带一", "带两", "带二", "带三", "带四", "带个",
                                "同行有", "同行的有", "有老人", "有小孩",
                                "with kids", "with children", "travelling with ", "traveling with "}) &&
            ends(short_next, {"老人", "小孩", "孩子", "婴儿", "宝宝", "小朋友", "宠物", "轮椅",
                              "kids", "children", "elderly parents", "a wheelchair"}) &&
            has(previous, {"旅行", "出游", "旅游", "行程", "攻略", "度假", "景点",
                           "trip", "itinerary", "travel", "vacation"})) return true;
        if (has(short_next, {"地铁", "车站", "公交", "机场", "市中心", "subway", "station", "airport"}) &&
            (starts(short_next, {"最好离", "尽量离", "离", "preferably near ", "close to "}) ||
             (short_next.size() <= 36 && ends(short_next, {"附近"}))) &&
            has(previous, {"推荐", "找", "选", "餐厅", "酒店", "住宿", "附近", "recommend",
                           "restaurant", "hotel", "find"})) return true;
        if (has(previous, {"天气", "气温", "温度", "weather", "forecast"}) &&
            !has(previous, {"写", "设计", "代码", "脚本", "程序", "write", "program", "script"}) &&
            bare_weather_location(short_next)) return true;
        if (starts(short_next, {"标准库", "原生", "内置", "自带", "框架", "标准框架",
                                "只用", "浏览器原生", "standard library ", "built-in "}) &&
            has(short_next, {"就行", "就好", "即可", "就可以", "is fine", "is enough"}) &&
            has(previous, {"代码", "脚本", "程序", "库", "框架", "python", "java",
                           "typescript", "javascript", "script", "program", "code"})) return true;
    }
    const bool content_task = has(previous, {"写", "翻译", "整理", "生成", "总结", "输出",
        "导出", "做", "制作", "文档", "报告", "表格", "改", "回答", "列出", "程序", "脚本", "代码",
        "write", "translate", "generate", "draft", "summarize", "format", "export",
        "document", "report", "table", "create", "answer", "list", "program", "script", "code"});
    if (kind == Parameter::language || kind == Parameter::format) return content_task;
    if (short_next.size() > 240 || !content_task) return false;

    const bool format_constraint = starts(short_next, {
        "格式用", "格式是", "格式要", "输出格式", "输出用", "语气", "字数", "篇幅",
        "用中文", "用英文", "用日文", "中文版", "英文版", "简短一点", "详细一点",
        "不用太", "不要太", "控制在", "限定在", "别超过", "最多", "至少", "保留原",
        "不要加", "不要用", "别加", "别用", "只要正文", "只要结论", "只给结论",
        "make it ", "keep it ", "use a ", "use the same ", "no more than ", "at most ",
        "at least ", "limit it to ", "without ", "format it ", "in plain text"});
    if (!format_constraint) return false;
    return !has(short_next, {"怎么", "为什么", "是什么", "能不能", "可不可以", "是否",
                             "帮我", "帮忙", "how ", "why ", "what ", "can you ", "could you "});
}
}  // namespace

extern "C" HAKIMI_EXPORT int hakimi_followup_relation(
    const char *old_text, std::size_t old_size,
    const char *new_text, std::size_t new_size) noexcept {
    if (!old_text || !new_text || old_size > max_bytes || new_size > max_bytes) return 0;
    try {
        return relation(normalize(View(old_text, old_size)), normalize(View(new_text, new_size)));
    } catch (...) {
        return 0;
    }
}
