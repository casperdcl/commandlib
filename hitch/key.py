from commandlib import run, CommandError
import hitchpython
from hitchstory import StoryCollection, StorySchema, BaseEngine, HitchStoryException
from hitchstory import validate, expected_exception
from hitchrun import expected
from commandlib import Command
from strictyaml import Str, Map, MapPattern, Int, Bool, Optional, load
from pathquery import pathq
import hitchtest
from hitchrun import hitch_maintenance
from commandlib import python
from hitchrun import DIR
from hitchrun.decorators import ignore_ctrlc
from hitchrunpy import ExamplePythonCode, HitchRunPyException, ExpectedExceptionMessageWasDifferent
import requests
from templex import Templex, NonMatching


class Engine(BaseEngine):
    """Python engine for running tests."""
    schema = StorySchema(
        given={
            Optional("scripts"): MapPattern(Str(), Str()),
            Optional("python version"): Str(),
            Optional("pexpect version"): Str(),
            Optional("icommandlib version"): Str(),
            Optional("setup"): Str(),
            Optional("files"): MapPattern(Str(), Str()),
            Optional("code"): Str(),
        },
        info={
            Optional("description"): Str(),
            Optional("importance"): Int(),
            Optional("docs"): Str(),
            Optional("fails on python 2"): Bool(),
        },
    )

    def __init__(self, keypath, settings):
        self.path = keypath
        self.settings = settings

    def set_up(self):
        """Set up your applications and the test environment."""
        self.path.state = self.path.gen.joinpath("state")
        if self.path.state.exists():
            self.path.state.rmtree(ignore_errors=True)
        self.path.state.mkdir()

        for script in self.given.get("scripts", []):
            script_path = self.path.state.joinpath(script)

            if not script_path.dirname().exists():
                script_path.dirname().makedirs()

            script_path.write_text(self.given['scripts'][script])
            script_path.chmod("u+x")

        for filename, contents in self.given.get("files", {}).items():
            self.path.state.joinpath(filename).write_text(contents)

        self.python_package = hitchpython.PythonPackage(
            self.given['python version']
        )
        self.python_package.build()

        self.pip = self.python_package.cmd.pip
        self.python = self.python_package.cmd.python

        # Install debugging packages
        with hitchtest.monitor([self.path.key.joinpath("debugrequirements.txt")]) as changed:
            if changed:
                run(self.pip("install", "-r", "debugrequirements.txt").in_dir(self.path.key))

        # Uninstall and reinstall
        with hitchtest.monitor(
            pathq(self.path.project.joinpath("commandlib")).ext("py")
        ) as changed:
            if changed:
                run(self.pip("uninstall", "commandlib", "-y").ignore_errors())
                run(self.pip("install", ".").in_dir(self.path.project))

        self.example_py_code = ExamplePythonCode(self.python, self.path.state)\
            .with_code(self.given.get('code', ''))\
            .with_setup_code(self.given.get('setup', ''))

    @expected_exception(NonMatching)
    @expected_exception(HitchRunPyException)
    @validate(
        code=Str(),
        will_output=Str(),
        raises=Map({
            Optional("type"): Map({"in python 2": Str(), "in python 3": Str()}) | Str(),
            Optional("message"): Map({"in python 2": Str(), "in python 3": Str()}) | Str(),
        })
    )
    def run(self, code, will_output=None, raises=None):
        to_run = self.example_py_code.with_code(code)

        if self.settings.get("cprofile"):
            to_run = to_run.with_cprofile(
                self.path.profile.joinpath("{0}.dat".format(self.story.slug))
            )

        result = to_run.expect_exceptions().run() if raises is not None else to_run.run()

        if will_output is not None:
            actual_output = '\n'.join([line.rstrip() for line in result.output.split("\n")])
            try:
                Templex(will_output).assert_match(actual_output)
            except NonMatching:
                if self.settings.get("rewrite"):
                    self.current_step.update(**{"will output": actual_output})
                else:
                    raise

        if raises is not None:
            differential = False  # Difference between python 2 and python 3 output?
            exception_type = raises.get('type')
            message = raises.get('message')

            if exception_type is not None:
                if not isinstance(exception_type, str):
                    differential = True
                    exception_type = exception_type['in python 2']\
                        if self.given['python version'].startswith("2")\
                        else exception_type['in python 3']

            if message is not None:
                if not isinstance(message, str):
                    differential = True
                    message = message['in python 2']\
                        if self.given['python version'].startswith("2")\
                        else message['in python 3']

            try:
                result = self.example_py_code.expect_exceptions().run()
                result.exception_was_raised(exception_type, message)
            except ExpectedExceptionMessageWasDifferent as error:
                if self.settings.get("rewrite") and not differential:
                    new_raises = raises.copy()
                    new_raises['message'] = result.exception.message
                    self.current_step.update(raises=new_raises)
                else:
                    raise

    def file_contents_will_be(self, filename, contents):
        file_contents = '\n'.join([
            line.rstrip() for line in
            self.path.state.joinpath(filename).bytes().decode('utf8').strip().split('\n')
        ])
        try:
            # Templex(file_contents).assert_match(contents.strip())
            assert file_contents == contents.strip(), \
                "{0} not {1}".format(file_contents, contents.strip())
        except NonMatching:
            if self.settings.get("rewrite"):
                self.current_step.update(contents=file_contents)
            else:
                raise

    def pause(self, message="Pause"):
        import IPython
        IPython.embed()

    def on_success(self):
        if self.settings.get("rewrite"):
            self.new_story.save()
        if self.settings.get("cprofile"):
            self.python(
                self.path.key.joinpath("printstats.py"),
                self.path.profile.joinpath("{0}.dat".format(self.story.slug))
            ).run()


def _storybook(settings):
    return StoryCollection(pathq(DIR.key).ext("story"), Engine(DIR, settings))


def _personal_settings():
    settings_file = DIR.key.joinpath("personalsettings.yml")

    if not settings_file.exists():
        settings_file.write_text((
            "engine:\n"
            "  rewrite: no\n"
            "  cprofile: no\n"
            "params:\n"
            "  python version: 3.5.0\n"
        ))
    return load(
        settings_file.bytes().decode('utf8'),
        Map({
            "engine": Map({
                "rewrite": Bool(),
                "cprofile": Bool(),
            }),
            "params": Map({
                "python version": Str(),
            }),
        })
    )


@expected(HitchStoryException)
def tdd(*keywords):
    """
    Run tests matching keywords.
    """
    settings = _personal_settings().data
    print(
        _storybook(settings['engine'])
        .with_params(**{"python version": settings['params']['python version']})
        .only_uninherited()
        .shortcut(*keywords).play().report()
    )


@expected(HitchStoryException)
def regressfile(filename):
    """
    Run all stories in filename 'filename' in python 2 and 3.

    Rewrite stories if appropriate.
    """
    print(
        _storybook({"rewrite": False}).in_filename(filename)
                                      .with_params(**{"python version": "2.7.10"})
                                      .ordered_by_name().play().report()
    )
    print(
        _storybook({"rewrite": False}).with_params(**{"python version": "3.5.0"})
                                      .in_filename(filename).ordered_by_name().play().report()
    )


@expected(HitchStoryException)
def regression():
    """
    Run regression testing - lint and then run all tests.
    """
    storybook = _storybook({}).only_uninherited()
    print(
        storybook.with_params(**{"python version": "2.7.10"})
                 .filter(lambda story: not story.info['fails on python 2'])
                 .ordered_by_name().play().report()
    )
    print(
        storybook.with_params(**{"python version": "3.5.0"}).ordered_by_name().play().report()
    )
    lint()


def lint():
    """
    Lint all code.
    """
    python("-m", "flake8")(
        DIR.project.joinpath("commandlib"),
        "--max-line-length=100",
        "--exclude=__init__.py",
    ).run()
    python("-m", "flake8")(
        DIR.key.joinpath("key.py"),
        "--max-line-length=100",
        "--exclude=__init__.py",
    ).run()
    print("Lint success!")


def hitch(*args):
    """
    Use 'h hitch --help' to get help on these commands.
    """
    hitch_maintenance(*args)


def deploy(version):
    """
    Deploy to pypi as specified version.
    """
    NAME = "commandlib"
    git = Command("git").in_dir(DIR.project)
    version_file = DIR.project.joinpath("VERSION")
    old_version = version_file.bytes().decode('utf8')
    if version_file.bytes().decode("utf8") != version:
        DIR.project.joinpath("VERSION").write_text(version)
        git("add", "VERSION").run()
        git("commit", "-m", "RELEASE: Version {0} -> {1}".format(
            old_version,
            version
        )).run()
        git("push").run()
        git("tag", "-a", version, "-m", "Version {0}".format(version)).run()
        git("push", "origin", version).run()
    else:
        git("push").run()

    # Set __version__ variable in __init__.py, build sdist and put it back
    initpy = DIR.project.joinpath(NAME, "__init__.py")
    original_initpy_contents = initpy.bytes().decode('utf8')
    initpy.write_text(
        original_initpy_contents.replace("DEVELOPMENT_VERSION", version)
    )
    python("setup.py", "sdist").in_dir(DIR.project).run()
    initpy.write_text(original_initpy_contents)

    # Upload to pypi
    python(
        "-m", "twine", "upload", "dist/{0}-{1}.tar.gz".format(NAME, version)
    ).in_dir(DIR.project).run()


@ignore_ctrlc
def ipy():
    """
    Run IPython in environment."
    """
    Command(DIR.gen.joinpath("py3.5.0", "bin", "ipython")).run()


def hvenvup(package, directory):
    """
    Install a new version of a package in the hitch venv.
    """
    pip = Command(DIR.gen.joinpath("hvenv", "bin", "pip"))
    pip("uninstall", package, "-y").run()
    pip("install", DIR.project.joinpath(directory).abspath()).run()


def rerun(version="3.5.0"):
    """
    Rerun last example code block with specified version of python.
    """
    Command(DIR.gen.joinpath("py{0}".format(version), "bin", "python"))(
        DIR.gen.joinpath("state", "examplepythoncode.py")
    ).in_dir(DIR.gen.joinpath("state")).run()
