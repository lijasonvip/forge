# Copyright 2017 datawire. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Forge CLI.

Usage:
  forge setup
  forge bake [-v] [--config=<config>]
  forge push [-v] [--config=<config>]
  forge manifest [-v] [--config=<config>]
  forge build [-v] [--config=<config>]
  forge deploy [-v] [--config=<config>] [--dry-run] [--namespace=<name>]
  forge -h | --help
  forge --version

Options:
  --config=<config>      Forge config file location.
  --filter=<pattern>     Only operate on services matching <pattern>. [default: *]
  -h --help              Show this screen.
  --version              Show version.
  -v,--verbose           Display more information.
  -n,--namespace=<name>  Deploy to specified namespace.
"""

from .tasks import (
    cull,
    get,
    project,
    setup,
    sh,
    status,
    summarize,
    sync,
    task,
    ERROR,
    Elidable,
    Secret,
    TaskError
)

setup()

import getpass

import click, base64, fnmatch, requests, os, sys, yaml
from dotenv import find_dotenv, load_dotenv
from collections import OrderedDict

import util
from . import __version__
from .service import Discovery, Service
from .docker import Docker
from .github import Github
from .kubernetes import Kubernetes
from .jinja2 import renders
from .istio import istio
from .output import Terminal
from scout import Scout

ENV = find_dotenv(usecwd=True)
if ENV: load_dotenv(ENV)

class CLIError(Exception): pass

SETUP_TEMPLATE = """# Global forge configuration
# DO NOT CHECK INTO GITHUB, THIS FILE CONTAINS SECRETS
docker-repo: {{docker}}
user: {{user}}
password: >
  {{password}}
"""

def file_contents(path):
    try:
        with open(os.path.expanduser(os.path.expandvars(path)), "read") as fd:
            return fd.read()
    except IOError, e:
        print "  %s" % e
        return None

class Forge(object):

    def __init__(self, verbose=0, config=None):
        self.verbose = verbose
        self.config = config or util.search_parents("forge.yaml")
        self.namespace = None
        self.dry_run = False
        self.terminal = Terminal()
        self.discovery = Discovery()
        self.services = OrderedDict()

    def prompt(self, msg, default=None, loader=None, echo=True):
        prompt = "%s: " % msg if default is None else "%s[%s]: " % (msg, default)
        prompter = raw_input if echo else getpass.getpass

        while True:
            value = prompter(prompt) or default
            if value is None: continue
            if loader is not None:
                loaded = loader(value)
                if loaded is None:
                    continue
            if loader:
                return value, loaded
            else:
                return value

    def setup(self):
        scout = Scout("forge", __version__)
        scout_res = scout.report()

        print self.terminal.bold("== Checking Kubernetes Setup ==")
        print

        checks = (("kubectl", "version", "--short"),
                  ("kubectl", "get", "service", "kubernetes", "--namespace", "default"))

        for cmd in checks:
            e = sh.run(*cmd)
            if e.result is ERROR:
                print
                raise CLIError(self.terminal.red("== Kubernetes Check Failed ==") +
                               "\n\nPlease make sure kubectl is installed/configured correctly.")

        registry = "registry.hub.docker.com"
        repo = None
        user = os.environ.get("USER", "")
        password = None
        json_key = None

        @task()
        def validate():
            dr = Docker(registry, repo, user, password)
            dr.validate()

        print
        print self.terminal.bold("== Setting up Docker ==")

        while True:
            print
            registry = self.prompt("Docker registry", registry)
            user = self.prompt("Docker user", user)
            repo = self.prompt("Docker organization", user)
            if user == "_json_key":
                json_key, password = self.prompt("Path to json key", json_key, loader=file_contents)
            else:
                password = self.prompt("Docker password", echo=False)

            print
            e = validate.run(task_include=lambda x: x.task.name in ('pull', 'push', 'tag'))
            if e.result is ERROR:
                print
                print self.terminal.red("-- please try again --")
                continue
            else:
                break

        print

        config = renders("SETUP_TEMPLATE", SETUP_TEMPLATE,
                         docker="%s/%s" % (registry, repo),
                         user=user,
                         password=base64.encodestring(password).replace("\n", "\n  "))

        config_file = "forge.yaml"

        print self.terminal.bold("== Writing config to %s ==" % config_file)

        with open(config_file, "write") as fd:
            fd.write(config)

        print
        print config.strip()
        print

        print self.terminal.bold("== Done ==")

    @task()
    def scan(self, directory):
        found = self.discovery.search(directory)
        for f in found:
            self.services[f.name] = f
        return [f.name for f in found]

    @task()
    def bake(self, service):
        status("checking if images exist")
        raw = list(cull(lambda c: not self.docker.exists(c.image, c.version), service.containers))
        baked = []
        if not raw:
            summarize("skipped, images exist")
            return baked

        for container in raw:
            status("building %s for %s " % (container.dockerfile, container.service.name))
            self.docker.build.go(container.abs_context, container.abs_dockerfile, container.image, container.version)
            baked.append(container.dockerfile)

        summarize("built %s" % (", ".join(c.dockerfile for c in raw)))
        return baked

    @task()
    def push(self, service):
        status("checking if %s containers exist" % service)
        unpushed = list(cull(lambda c: self.docker.needs_push(c.image, c.version), service.containers))

        pushed = []
        if not unpushed:
            summarize("skipped, images exist")
            return []

        for container in unpushed:
            status("pushing container %s" % container.dockerfile)
            pushed.append(self.docker.push(container.image, container.version))

        summarize("pushed %s" % ", ".join(c.dockerfile for c in unpushed))
        return pushed

    def template(self, svc):
        k8s_dir = os.path.join(svc.root, ".forge", "k8s", svc.name)
        svc.deployment(self.docker.registry, self.docker.namespace, k8s_dir)
        return k8s_dir, self.kube.resources(k8s_dir)

    @task()
    def manifest(self, service):
        status("generating manifests for %s" % service.name)
        k8s_dir, resources = self.template(service)
        istioify = service.info().get("istio", False)
        if istioify:
            status("istioifying kube manifests")
            istio(k8s_dir)
        summarize("generated %s\nwrote %smanifests to %s" % (", ".join(str(r) for r in resources),
                                                             "istioified " if istioify else "",
                                                             k8s_dir))
        return k8s_dir

    @task()
    def build(self, service):
        baked = self.bake(service)
        pushed = self.push(service)
        result = self.manifest(service)

        lines = []
        if baked:
            lines.append("%s" % ", ".join(baked))
        if pushed:
            lines.append("%s %s" % (self.terminal.green("pushed"), (", ".join(pushed))))
        lines.append("%s %s" % (self.terminal.green("manifests"), result))

        summarize("\n".join(lines))
        return result

    @task()
    def deploy(self, k8s_dir):
        result = self.kube.apply(k8s_dir)
        code = self.terminal.green("OK") if result.code == 0 else self.terminal.red("ERR[%s]" % result.code)
        summarize("%s -> %s\n%s" % (" ".join(result.command), code, result.output))

    def load_config(self):
        if not self.config:
            raise CLIError("unable to find forge.yaml, try running `forge setup`")

        with open(self.config, "read") as fd:
            conf = yaml.load(fd)

        self.base = os.path.dirname(os.path.abspath(self.config))
        self.docker = get_docker(conf)

        self.kube = Kubernetes(namespace=self.namespace, dry_run=self.dry_run)

    def load_services(self, deps=False):
        start = util.search_parents("service.yaml")
        if start:
            path = os.path.dirname(start)
        else:
            path = os.getcwd()
        services = self.scan(path)
        if not os.path.samefile(path, self.base):
            self.scan(self.base)
        if services:
            services.extend(self.discovery.dependencies(services))
        return services

    @task()
    def metadata(self):
        self.load_config()
        services = self.load_services(deps=False)
        if not services:
            raise TaskError("no service found")
        else:
            svc = self.services[services[0]]
            print yaml.dump(svc.metadata(self.docker.registry, self.docker.namespace))

    def execute(self, goal):
        self.load_config()

        @task()
        def service(name):
            svc = self.services[name]
            goal(svc)
            summarize(self.terminal.white(name))

        @task("forge")
        def root():
            for name in self.load_services(deps=True):
               service.go(name)

        INCLUDED = set(["scan", "dependencies", "service", "build", "deploy"])
        if self.verbose:
            INCLUDED.update(["GET", "CMD"])

        root.run(task_include=lambda x: x.task.name in INCLUDED)

def get_password(conf):
    pw = conf.get("password")
    if not pw:
        raise CLIError("docker password must be configured")
    return base64.decodestring(pw)

def get_docker(conf):
    url = conf.get("docker-repo")

    if url is None:
        raise CLIError("docker-repo must be configured")
    if "/" not in url:
        raise CLIError("docker-repo must be in the form <registry-url>/<namespace>")
    registry, namespace = url.split("/", 1)

    try:
        user = conf["user"]
    except KeyError, e:
        raise CLIError("missing config property: %s" % e)

    return Docker(registry, namespace, user, get_password(conf))

@click.group()
@click.version_option(__version__, message="%(prog)s %(version)s")
@click.option('-v', '--verbose', count=True)
@click.option('--config', envvar='FORGE_CONFIG', type=click.Path(exists=True))
@click.pass_context
def forge(context, verbose, config):
    context.obj = Forge(verbose=verbose, config=config)

@forge.command()
@click.pass_obj
def setup(forge):
    """
    Help with first time setup of forge.

    Forge needs access to a container registry and a kubernetes
    cluster in order to deploy code. This command helps setup and
    validate the configuration necessary to access these resources.
    """
    return forge.setup()

@forge.group(invoke_without_command=True)
@click.pass_context
@click.option('-n', '--namespace', envvar='K8S_NAMESPACE', type=click.STRING)
@click.option('--dry-run', is_flag=True)
def build(ctx, namespace, dry_run):
    """Build deployment artifacts for a service.

    Deployment artifacts for a service consist of the docker
    containers and kubernetes manifests necessary to run your
    service. Forge automates the process of building your containers
    from source and producing the manifests necessary to run those
    newly built containers in kubernetes. Use `forge build
    [containers|manifests]` to build just containers, just manifests,
    or (the default) all of the above.

    How forge builds containers:

    By default every `Dockerfile` in your project is built and tagged
    with a version computed from the input sources. You can customize
    how containers are built using service.yaml. The `containers`
    property of `service.yaml` lets you specify an array.

    \b
    name: my-service
    ...
    container:
     - dockerfile: path/to/Dockerfile
       context: context/path
       args:
        MY_ARG: foo
        MY_OTHER_ARG: bar

    How forge builds deployment manifests:

    The source for your deployment manifests are kept as jinja
    templates in the k8s directory of your project. The final
    deployment templates are produced by rendering these templates
    with access to relevant service and build related metadata.

    You can use the `forge build metadata` command to view all the
    metadata available to these templates. See the `forge metadata`
    help for more info.

    """
    forge = ctx.obj
    forge.namespace = namespace
    forge.dry_run = dry_run
    if ctx.invoked_subcommand is None:
        forge.execute(forge.build)

@build.command()
@click.pass_obj
def metadata(forge):
    """
    Display build metadata.

    This command outputs all the build metadata available to manifests.
    """
    forge.metadata()

@build.command()
@click.pass_obj
def containers(forge):
    """
    Build containers for a service.

    See `forge build --help` for details on how containers are built.
    """
    forge.execute(forge.bake)

@build.command()
@click.pass_obj
def manifests(forge):
    """
    Build manifests for a service.

    See `forge build --help` for details on how manifests are built.
    """
    forge.execute(forge.manifest)

@forge.command()
@click.pass_obj
@click.option('-n', '--namespace', envvar='K8S_NAMESPACE', type=click.STRING)
@click.option('--dry-run', is_flag=True)
def deploy(forge, namespace, dry_run):
    """
    Build and deploy a service.

    They deploy command performs a `forge build` and then applies the
    resulting deployment manifests using `kubectl apply`.
    """
    forge.namespace = namespace
    forge.dry_run = dry_run
    forge.execute(lambda svc: forge.deploy(forge.build(svc)))

def call_main():
    util.setup_yaml()
    try:
        exit(forge())
    except CLIError, e:
        exit(e)
    except KeyboardInterrupt, e:
        exit(e)

if __name__ == "__main__":
    call_main()
