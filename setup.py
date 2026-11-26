from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


class BuildFollowup(build_ext):
    def build_extensions(self):
        module, = self.extensions
        module.extra_compile_args = (
            ["/std:c++17", "/utf-8", "/EHsc"] if self.compiler.compiler_type == "msvc"
            else ["-std=c++17", "-fvisibility=hidden"]
        )
        super().build_extensions()


setup(
    ext_modules=[Extension(
        "hakimi.bridge._followup_rules",
        ["src/hakimi/bridge/followup.cpp", "src/hakimi/bridge/followup_module.cpp"],
        language="c++", optional=True,
    )],
    cmdclass={"build_ext": BuildFollowup},
)
