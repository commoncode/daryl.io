#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# First draft of a port of our Django–centric module style fabfile so
# it's suitable for use in deploying Meteor applications.
#
# It looks to a python module (pointed to by HOST_ROLES env. var)
# for roles (groups of hosts) to work on.
#
# Examples:
#   `HOST_ROLES=serverroles fab deploy`
#        will prompt for role selection from the serverroles python module.
#   `HOST_ROLES=serverroles fab -R staging deploy`
#        would deploy to whatever staging servers are setup.
#   `HOST_ROLES=serverroles fab -H mytests.local`
#        would deploy to the single host mytest.local.
#   `HOST_ROLES=serverroles fab deploy`
#        will prompt for role selection before deploying.

import os
import subprocess
import sys
from fabric.api import abort, env, hide, local, task
from fabric.context_managers import cd, lcd
from fabric.contrib.console import confirm
from fabric.decorators import runs_once
from fabric.operations import prompt, run, sudo


try:
    from camplight import Request, Campfire
    print 'imported camplight'
    camplight = True
except ImportError:
    camplight = False


# defaults for conforming puppet-managed vhost instances
DEFAULT_VHOST_PATH = '/home/vhosts/'
DEFAULT_REPO_NAME = 'repo.git' # a bare repo sent to with send-pack
DEFAULT_WORKTREE = 'code'
DEFAULT_METEOR = True
DEFAULT_RUNDIR = 'rundir'


try:
    host_roles = os.environ['HOST_ROLES']
except KeyError:
    host_roles = "roles"

# set env.vhosts from the python module…
try:
    fab_roles = __import__(host_roles)
except ImportError:
    raise RuntimeError("Couldn't import your project roles!")

vhosts = getattr(fab_roles, 'vhosts', None)

env.forward_agent = True


if vhosts != None:
    for vhost in vhosts.keys():
        vhosts[vhost]['vhostpath'] = \
            vhosts[vhost].get('vhostpath', DEFAULT_VHOST_PATH + vhost)
        vhosts[vhost]['reponame'] = \
            vhosts[vhost].get('reponame', DEFAULT_REPO_NAME)
        vhosts[vhost]['worktree'] = \
            vhosts[vhost].get('worktree', DEFAULT_WORKTREE)
        vhosts[vhost]['meteor'] = \
            vhosts[vhost].get('meteor', DEFAULT_METEOR)
        vhosts[vhost]['rundir'] = \
            vhosts[vhost].get('rundir', DEFAULT_RUNDIR)
    env.vhosts = vhosts

    # env.roledefs is used internally by Fabric, so preserve that behaviour
    for vhost in env.vhosts.keys():
        env.roledefs.update({
                vhost: env.vhosts[vhost]['hosts']
                })

    # only prompt for a role later if we're not running a side-effect free
    # command.
    do_something = True
    quick_cmds = ('-l', '--list', 'check_clean', 'listroles')
    for arg in sys.argv:
        if arg in quick_cmds:
            do_something = False
            continue

# If Fabric is called without specifying either a *role* (group of
# predefined servers) or at least one *host* (via the -H argument),
# then prompt the user to choose a role from the predefined roledefs
# list.  This way, the env.hosts list is constructed at script load
# time and all the functions can use it when they run (by the time
# fabric commands are run the environment should already be set up
# with all the required host information!).
if vhosts is None:
    pass
elif do_something and (not env.roles and not env.hosts):
    validgrp = prompt("Choose host group [%s]: " % \
                          ", ".join(env.roledefs.keys()),
                      validate=lambda x: x in env.roledefs.keys() and x)
    if not validgrp:
        abort('No such group of hosts.')
    if hasattr(env.roledefs[validgrp], '__call__'):
        # if the role definition value is callable, call it to get the
        # list of hosts.
        print "Retrieving list of hosts",
        sys.stdout.flush()
        rawhosts = env.roledefs[validgrp]()
        hosts = [host['address'] for host in rawhosts]
        hostnames = [host['name'] for host in rawhosts]
        print "OK"
    else:
        hostnames = hosts = env.roledefs[validgrp]
        env.hosts.extend(hosts)
    if not confirm("Acting on the following hosts: \n%s\nOK? " \
                       % "\n".join(hostnames)):
        abort('OK, aborting.')
    # env.roles used by Fabric internally
    env.roles = []
    env.vhost = validgrp
    env.hosts = hosts
elif len(env.roles) > 1:
    # simplifies host detection for now…
    abort('Sorry, I currently only operate on one role at a time')
elif env.roles:
    role = env.roles[0]
    print "Retrieving list of hosts for role %s" % role
    if hasattr(env.roledefs[role], '__call__'):
        # if the role definition value is callable, call it to get the
        # list of hosts.
        sys.stdout.flush()
        rawhosts = env.roledefs[role]()
        hosts = [host['address'] for host in rawhosts]
        hostnames = [host['name'] for host in rawhosts]
        env.roles = []
        env.vhost = role
        env.hosts = hosts
    else:
        hosts = env.roledefs[role]
        env.vhost = role
        env.hosts.extend(hosts)
    print "OK"
elif env.hosts:
    # hosts specified on the commandline…
    # makes things saner if we only allow hosts already declared in
    # our vhosts, since we need a vhostpath.  And only hosts from a
    # single Role can be specified.
    print "Checking sanity of manual host selection",
    sys.stdout.flush()
    # make sure all hosts specified belong to a Role, and only one
    # Role.  Since to do this we need to resolve all role
    # hostnames, it might take a little while…
    hostlist = {}
    for vhost in env.vhosts.keys():
        hostlist[vhost] = []
        if hasattr(env.vhosts[vhost]['hosts'], '__call__'):
            hostlist[vhost].extend(env.vhosts[vhost]['hosts']())
        else:
            hostlist[vhost].append({
                    'address': env.vhosts[vhost]['hosts'][0],
                    'name': env.vhosts[vhost]['hosts'][0]
                    })
    # now check supplied hosts against list of all hosts from all roles
    role = None
    ##
    ## env.hosts might contain short names, like 'K3-App-1', so
    ## resolve those to their IP addresses; rewriting env.hosts
    ## accordingly.
    ##
    for i, host in enumerate(env.hosts): # hosts from commandline
        for vhost in hostlist.keys():
            for host_dict in hostlist[vhost]:
                if host in host_dict['address']:
                    # the role this host belongs to
                    if role is None:
                        role = vhost
                    elif role != vhost:
                        abort("Sorry, only hosts for a single role can be provided")
                    # we've got a role for the provided host
                    continue
                elif host in host_dict['name']:
                    env.hosts[i] = host_dict['address']
                    # the role this host belongs to
                    if role is None:
                        role = vhost
                    elif role != vhost:
                        abort("Sorry, only hosts for a single role can be provided")
                    # we've got a role for the provided host
                    continue
    if role is None:
        abort("Sorry, only hosts from a declared role can be provided")
    else:
        env.vhost = role
    print "OK"


#
## Commands start here
#
# used when checking for a clean local worktree
PROJECT_PATH = os.path.realpath(os.path.join(os.path.dirname(__file__), '..'))


@task
def kickpuppy():
    """Runs a 'service puppet restart'.
    """
    sudo('/usr/sbin/service puppet restart')


@task
def chownvhost():
    """Ensures various directories are owned by www-data:staff.
    """
    with hide('running'):
        sudo('/bin/chown -R www-data:staff %s/%s' % (env.vhosts[env.vhost]['vhostpath'],
            env.vhosts[env.vhost]['worktree']))
        sudo('/bin/chown -R www-data:staff %s/%s' % (env.vhosts[env.vhost]['vhostpath'],
            env.vhosts[env.vhost]['reponame']))
        sudo('/bin/chown -R www-data:staff %s/%s' % (env.vhosts[env.vhost]['vhostpath'],
            env.vhosts[env.vhost]['rundir']))


@task
def deploy():
    """Deploy code to hosts and restart services.
    """
    # check_clean()
    refspec = os.getenv('GIT_REFSPEC', False)
    revision = os.getenv('GIT_COMMIT', False)
    if not refspec or not revision:
        with lcd(PROJECT_PATH):
            # determine refspec and revision using git plumbing.
            refspec = local("git symbolic-ref HEAD",
                            capture=True).strip()
            commit_msg = local("git log -n 1 --oneline",
                             capture=True).strip()
    chownvhost()
    light_a_campfire()
    tell_campfire('{} deploying {} ({}) to [{}]'.format(
        env.user,
        refspec,
        commit_msg,
        env.vhost))
    pull()
    chownvhost()
    mrt_deploy()
    chownvhost()
    restartservices()
    tell_campfire('{} deployed {} ({}) to [{}]'.format(
        env.user,
        refspec,
        commit_msg,
        env.vhost))


@task
def mrt_deploy():
    """Bundles & unbundles on the server.

    The stuff in the worktree ends up in the rundir, by means of
    meteorite bundling.
    """
    # bundle from the checked out code (worktree)
    with cd('%s/%s' % (env.vhosts[env.vhost]['vhostpath'],
                       env.vhosts[env.vhost]['worktree'])):
        with hide('running', 'stdout'):
            sudo('/bin/chown -R www-data:staff .')
            sudo('/bin/chmod -R g+w .')
            run('rm -f /tmp/bundle_%s' % env.vhost)
            # copy a couple of files so they make it into the bundle
        run('test -f ../secrets/server_local_settings.js && cp ../secrets/server_local_settings.js app/server/_local_settings.js || true')
        run('test -f ../secrets/lib_local_settings.js && cp ../secrets/lib_local_settings.js app/lib/_local_settings.js || true')
        with cd('app'):
            run('mrt bundle /tmp/bundle_%s.tar.gz' % env.vhost)

    # unbundle inside the rundir
    with cd('%s/%s' % (env.vhosts[env.vhost]['vhostpath'],
                       env.vhosts[env.vhost]['rundir'])):
        sudo('rm -rf bundle')
        run('tar xfz /tmp/bundle_%s.tar.gz' % env.vhost)

    # NOTE: the path to node_modules changes in Meteor 0.6.5
    #       bundle/programs/server/node_modules
    with cd('%s/%s/bundle/server/node_modules' % (
            env.vhosts[env.vhost]['vhostpath'],
            env.vhosts[env.vhost]['rundir'])):
        # reinstall fibers
        run('npm uninstall fibers')
        run('npm install fibers')
    #
    # End npm packaging hack.
    #

    with cd('%s/%s' % (env.vhosts[env.vhost]['vhostpath'],
                       env.vhosts[env.vhost]['rundir'])):
        with hide('running', 'stdout'):
            sudo('/bin/chown -R www-data:staff bundle')
            sudo('/bin/chmod -R g+w bundle')

    # delete the temp bundle .tar.gz
    run('rm -f /tmp/bundle_%s.tar.gz' % env.vhost)


@task
@runs_once
def check_clean():
    """Check for clean working tree.

    Uses “non-porcelain” Git commands (i.e. it uses “plumbing”
    commands), which are supposed to be much more stable than user
    interface commands.
    """
    print "Checking for a clean tree "
    # update the index first
    with hide('running'):
        with lcd(PROJECT_PATH):
            local('git update-index -q --ignore-submodules --refresh')
        # 1. check for unstaged changes in the working tree
        rtncode = subprocess.call(['git', 'diff-files', '--quiet',
                                   '--ignore-submodules', '--'],
                                  cwd=PROJECT_PATH)
        if rtncode:
            # Python < 2.7 doesn't have subprocess.check_call :(
            process = subprocess.Popen(['git', 'diff-files',
                                        '--name-status', '-r',
                                        '--ignore-submodules', '--'],
                                       stdout=subprocess.PIPE,
                                       cwd=PROJECT_PATH)
            output, err = process.communicate()
            print '\n\n%s' % output.strip()
            abort('Resolve your unstaged changes before deploying!')
        # 2. check for uncommitted changes in the index
        rtncode = subprocess.call(['git', 'diff-index', '--cached',
                                   '--quiet', 'HEAD',
                                   '--ignore-submodules', '--'],
                                  cwd=PROJECT_PATH)
        if rtncode:
            # Python < 2.7 doesn't have subprocess.check_call :(
            process = subprocess.Popen(['git', 'diff-index', '--cached',
                                        '--name-status', '-r',
                                        '--ignore-submodules',
                                        'HEAD', '--'],
                                       stdout=subprocess.PIPE,
                                       cwd=PROJECT_PATH)
            output, err = process.communicate()
            print '\n\n%s' % output.strip()
            abort('Resolve your uncommitted changes before deploying!')
        # 3. check for untracked files in the working tree
        process = subprocess.Popen(['git', 'ls-files', '--others',
                                    '--exclude-standard', '--error-unmatch',
                                    '--'],
                                   stdout=subprocess.PIPE,
                                   cwd=PROJECT_PATH)
        output, err = process.communicate()
        if output:
            print '\n\n%s' % output.strip()
            abort('Resolve your untracked files before deploying!')
        # 4. check the refspec and commit to ensure it's on the origin
        # server (so can be pulled onto the deployment target)
        refspec = os.getenv('GIT_REFSPEC', False)
        revision = os.getenv('GIT_COMMIT', False)
        if not refspec or not revision:
            with lcd(PROJECT_PATH):
                # determine refspec and revision using git plumbing.
                refspec = local("git symbolic-ref HEAD",
                                capture=True).strip()
                revision = local("git rev-parse --verify HEAD",
                                 capture=True).strip()
        print 'Fetching origin refs'
        local('git fetch origin')
        process = subprocess.Popen(['git', 'branch', '-r', '--contains', revision],
                                   stdout=subprocess.PIPE,
                                   cwd=PROJECT_PATH)
        output, err = process.communicate()
        if not output:
            abort("The revision you're trying to deploy doesn't exist in the origin.  You have to push.")
    print "OK"


@task
def checkouttag(tag):
    """Checks out a tag from the repository into the worktree.
    """
    with cd('%s/%s' % (env.vhosts[env.vhost]['vhostpath'],
                       env.vhosts[env.vhost]['reponame'])):
        run('git fetch --tags')
        # delete the old worktree before checking out fresh
        sudo('/bin/chmod -R g+w %s/%s/' % (env.vhosts[env.vhost]['vhostpath'],
                                            env.vhosts[env.vhost]['worktree']))
        run('rm -rf %s/%s/*' % (env.vhosts[env.vhost]['vhostpath'],
                                env.vhosts[env.vhost]['worktree']))
        sudo('/bin/chmod -R g+w .')
        sudo('/usr/bin/git checkout -f %s' % tag)
        sudo('/bin/chmod -R g+w %s/%s/' % (env.vhosts[env.vhost]['vhostpath'],
                                            env.vhosts[env.vhost]['worktree']))
    print "OK"


@task
def pull():
    """Fetch and checkout the revision from the repo.
    """
    with hide():
        refspec = os.getenv('GIT_REFSPEC', False)
        revision = os.getenv('GIT_COMMIT', False)
        if not refspec or not revision:
            with lcd(PROJECT_PATH):
                # determine refspec and revision using git plumbing.
                refspec = local("git symbolic-ref HEAD",
                                capture=True).strip()
                revision = local("git rev-parse --verify HEAD",
                                 capture=True).strip()
        with cd('%s/%s' % (env.vhosts[env.vhost]['vhostpath'],
                           env.vhosts[env.vhost]['reponame'])):
            run('git fetch origin %s' % refspec)
            # delete the old worktree before checking out fresh
            sudo('/bin/chmod -R g+w %s/%s/' % (env.vhosts[env.vhost]['vhostpath'],
                                                env.vhosts[env.vhost]['worktree']))
            run('rm -rf %s/%s/*' % (env.vhosts[env.vhost]['vhostpath'],
                                  env.vhosts[env.vhost]['worktree']))
            sudo('/bin/chmod -R g+w .')
            sudo('/usr/bin/git checkout -f %s' % revision)
            sudo('/bin/chown -R www-data:staff %s/%s' % (
                env.vhosts[env.vhost]['vhostpath'],
                env.vhosts[env.vhost]['reponame']))
    print "OK"


@task
def restartservices():
    """Restart web workers.
    """
    with hide('running'):
        with cd(env.vhosts[env.vhost]['vhostpath']):
            print "Restarting %s " % env.host
            if env.vhosts[env.vhost]['meteor']:
                run("supervisorctl restart %s_meteor" % env.vhost)
    print "OK"


@task
def stopservices():
    """Stop services.
    """
    with hide('running'):
        with cd(env.vhosts[env.vhost]['vhostpath']):
            print "Stopping %s " % env.host
            if env.vhosts[env.vhost]['meteor']:
                run("supervisorctl stop %s_meteor" % env.vhost)
    print "OK"


@task
def startservices():
    """Start services.
    """
    with hide('running'):
        with cd(env.vhosts[env.vhost]['vhostpath']):
            print "Starting %s " % env.host
            if env.vhosts[env.vhost]['meteor']:
                run("supervisorctl start %s_meteor" % env.vhost)
    print "OK"


@task
def listroles():
    """Lists the roles defined in HOST_ROLES module.
    """
    print 'I know about the following roles: %s' % \
        ', '.join(env.vhosts.keys())




@task
@runs_once
def light_a_campfire():

    if camplight:

        try:
            #
            # Light a campfire.
            #
            # XXX Hardcode values should be abstracted to settings
            #
            campfire_request = Request(
                'https://commoncode.campfirenow.com',
                '6c1897e6a194951ea55c82c05e18b79a3562e1e6'
            )
            campfire = Campfire(campfire_request)
            account = campfire.account()
            rooms = campfire.rooms()
            global campfire_is_lit
            global campfire_room
            campfire_room = campfire.room('Mentorloop')
            campfire_room.join()

        except Exception, e:
            #
            # Log these for now.  We're expecting a HttpError due to connection
            # problems, or a changed API token.
            #
            print 'Error: %s' % e
            campfire_is_lit = False

        else:
            #
            # The campfire is lit!
            #

            campfire_is_lit = True


def tell_campfire(msg):
    if camplight and campfire_is_lit:
        campfire_room.speak(msg)
