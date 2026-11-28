# Build the media-reference compactor used by hakimi.prompt.media_codes.
#   make -C native/hkmedia          -> build/libhkmedia.so and build/hkmedia
# Without this library Hakimi falls back to the identical Python mirror.
CXX ?= g++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra -fPIC -fvisibility=hidden
BUILD := build

all: $(BUILD)/libhkmedia.so $(BUILD)/hkmedia

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/libhkmedia.so: hkmedia.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -fvisibility=default -shared -o $@ $<

$(BUILD)/hkmedia: hkmedia_cli.cpp hkmedia.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ hkmedia_cli.cpp hkmedia.cpp

clean:
	rm -rf $(BUILD)

.PHONY: all clean
