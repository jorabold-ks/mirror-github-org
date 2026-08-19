import sys
import os
import time
import datetime
import urllib.parse

from github import Github
from github.GithubException import UnknownObjectException, GithubException

RATE_BUFFER = 100
EXTRA_WAIT = 60

FORK_RETRY_WAIT = 60   # seconds to wait on secondary rate limit before retrying
FORK_MAX_RETRIES = 5   # maximum number of retries for fork creation


def check_rate_limiting(rl):
    remaining, total = rl._requester.rate_limiting

    if remaining < RATE_BUFFER:
        reset_time = rl._requester.rate_limiting_resettime
        reset_time_human = datetime.datetime.fromtimestamp(
            int(reset_time)
        ) + datetime.timedelta(seconds=EXTRA_WAIT)

        print(
            "\nWAITING: Remaining rate limit is %s of %s. Waiting %s mins for reset at %s before continuing.\n"
            % (remaining, total, int((reset_time - time.time()) / 60), reset_time_human)
        )

        while time.time() <= (reset_time + EXTRA_WAIT):
            time.sleep(60)
            print(".", end="")

        print("\n")


def create_fork_with_retry(dst_org, src_repo):
    """Fork a repo, retrying with exponential backoff on secondary rate limit (403)."""
    wait = FORK_RETRY_WAIT
    for attempt in range(1, FORK_MAX_RETRIES + 1):
        try:
            return dst_org.create_fork(src_repo)
        except GithubException as e:
            data = e._GithubException__data
            message = data.get("message", "") if isinstance(data, dict) else str(data)

            if "contains no Git content" in message:
                print("\n * Skipping empty repository", end="")
                return None

            if e.status == 403 and "submitted too quickly" in message:
                if attempt < FORK_MAX_RETRIES:
                    print(
                        "\n * Secondary rate limit hit (attempt %d/%d). Waiting %ds before retrying..."
                        % (attempt, FORK_MAX_RETRIES, wait),
                        end="",
                    )
                    time.sleep(wait)
                    wait *= 2  # exponential backoff
                    continue
                else:
                    print(
                        "\n * Secondary rate limit hit. Exhausted %d retries, skipping %s."
                        % (FORK_MAX_RETRIES, src_repo.name),
                        end="",
                    )
                    return None

            raise e


def mirror(token, src_org, dst_org, full_run=False):
    g = Github(token)

    src_org = g.get_organization(src_org)
    dst_org = g.get_organization(dst_org)

    for src_repo in src_org.get_repos("public", sort="pushed", direction="desc"):
        check_rate_limiting(src_repo)

        dst_repo = None
        try:
            dst_repo = dst_org.get_repo(src_repo.name)
        except UnknownObjectException:
            pass

        if not dst_repo:
            print("\n\nForking %s..." % src_repo.name, end="")
            create_fork_with_retry(dst_org, src_repo)

        else:
            print("\n\nSyncing %s..." % src_repo.name, end="")

            updated = False
            for src_branch in src_repo.get_branches():
                check_rate_limiting(src_branch)

                print("\n - %s " % src_branch.name, end=""),
                encoded_name = urllib.parse.quote(src_branch.name)

                if src_branch.name.startswith("dependabot/"):
                    print("(skipping)", end="")
                    continue

                try:
                    dst_ref = dst_repo.get_git_ref(ref="heads/%s" % encoded_name)
                except UnknownObjectException:
                    dst_ref = None

                try:
                    if dst_ref and dst_ref.object:
                        if src_branch.commit.sha != dst_ref.object.sha:
                            print("(updated)", end="")
                            dst_ref.edit(sha=src_branch.commit.sha, force=True)
                            updated = True
                    else:
                        print("(new)", end="")
                        dst_repo.create_git_ref(
                            ref="refs/heads/%s" % encoded_name, sha=src_branch.commit.sha
                        )
                        updated = True

                except GithubException as e:
                    if e.status == 422:
                        print("\n * Github API hit a transient validation error, ignoring for now: ", e, end="")
                    else:
                        raise e

            if not full_run and not updated:
                print("\n\nNo more updates to mirror. Ending run.")
                sys.exit(0)


if __name__ == "__main__":
    p = {}
    for param in ("GITHUB_TOKEN", "SRC_ORG", "DST_ORG"):
        p[param] = os.getenv(param)
        if not p[param]:
            print("No %s supplied in env" % param)
            sys.exit(1)

    full_run=False
    if "--full-run" in sys.argv:
        print("Doing a full run, will check all repositories and branches - This may take a long time")
        full_run = True

    mirror(p["GITHUB_TOKEN"], p["SRC_ORG"], p["DST_ORG"], full_run=full_run)
