from __future__ import annotations

import concurrent.futures
import datetime as dt
import functools
import git
import pathlib
import pickle
import re
import time
import warnings

import rich.console
import rich.live
import rich.syntax

import lea


RUNNING = "[cyan]RUNNING"
SUCCESS = "[green]SUCCESS"
ERRORED = "[red]ERRORED"
SKIPPED = "[yellow]SKIPPED"


def _do_nothing(*args, **kwargs):
    """This is a dummy function for dry runs"""


def pretty_print_view(view: lea.views.View, console: rich.console.Console) -> str:
    if isinstance(view, lea.views.SQLView):
        syntax = rich.syntax.Syntax(view.query, "sql")
    elif isinstance(view, lea.views.PythonView):
        syntax = rich.syntax.Syntax(view.path.read_text(), "python")
    else:
        raise NotImplementedError
    console.print(syntax)


def _make_table_reference_mapping(dag: lea.views.DAGOfViews, client: lea.clients.Client, selected_view_keys: set[tuple[str]], freeze_unselected: bool) -> dict[str, str]:
    """

    There are two types of table_references: those that refer to a table in the current database,
    and those that refer to a table in another database. The next goal is to determine how to
    rename the table references in each view.

    """

    # By default, we replace all
    # table_references to the current database, but we leave the others untouched.
    if not freeze_unselected:
        return {
            client._view_key_to_table_reference(view_key): client._view_key_to_table_reference(view_key, with_username=True)
            for view_key in dag
        }

    # When freeze_unselected is specified, it means we want our views to target the production
    # database. Therefore, we only have to rename the table references for the views that were
    # selected.

    # Note the case where the select list is empty. That means all the views should be refreshed.
    # If freeze_unselected is specified, then it means all the views will target the production
    # database, which is basically equivalent to copying over the data.
    if not select:
        warnings.warn("Setting freeze_unselected without selecting views is not encouraged")
    table_reference_mapping = {
        client._view_key_to_table_reference(view_key): client._view_key_to_table_reference(view_key, with_username=True)
        for view_key in selected_view_keys
    }

def run(
    client: lea.clients.Client,
    views_dir: pathlib.Path,
    select: list[str],
    freeze_unselected: bool,
    print_views: bool,
    dry: bool,
    silent: bool,
    fresh: bool,
    threads: int,
    show: int,
    fail_fast: bool,
    console: rich.console.Console,
):

    # If print_to_cli, it means we only want to print out the view definitions, nothing else
    silent = print_views or silent
    console_log = _do_nothing if silent else console.log

    # Organize the views into a directed acyclic graph
    views = client.open_views(views_dir)
    views = [view for view in views if view.schema not in {"tests", "funcs"}]
    dag = client.make_dag(views)

    # Let's determine which views need to be run
    selected_view_keys = dag.select(*select) if select else dag.keys()

    # Let the user know the views we've decided which views will run
    views_sp = "views" if len(selected_view_keys) > 1 else "view"
    console_log(f"{len(selected_view_keys):,d} {views_sp} out of {len(views):,d} selected")

    # Now we determine the table reference mapping
    table_reference_mapping = _make_table_reference_mapping(
        dag=dag,
        client=client,
        selected_view_keys=selected_view_keys,
        freeze_unselected=freeze_unselected
    )

    # Remove orphan views
    for table_reference in client.list_tables()["table_reference"]:
        view_key = client._table_reference_to_view_key(table_reference)
        if view_key in dag:
            continue
        if not dry:
            client.delete_view_key(view_key)
        console_log(f"Removed {table_reference}")

    def display_progress() -> rich.table.Table:
        if silent:
            return None
        table = rich.table.Table(box=None)
        table.add_column("#", header_style="italic")
        table.add_column("view", header_style="italic")
        table.add_column("status", header_style="italic")
        table.add_column("duration", header_style="italic")

        not_done = [view_key for view_key in execution_order if view_key not in cache]
        for i, view_key in list(enumerate(not_done, start=1))[-show:]:
            if view_key in exceptions:
                status = ERRORED
            elif view_key in skipped:
                status = SKIPPED
            elif view_key in jobs_ended_at:
                status = SUCCESS
            else:
                status = RUNNING
            duration = (
                (jobs_ended_at.get(view_key, dt.datetime.now()) - jobs_started_at[view_key])
                if view_key in jobs_started_at
                else None
            )
            # Round to the closest second
            duration_str = f"{int(round(duration.total_seconds()))}s" if duration else ""
            table.add_row(str(i), str(dag[view_key]), status, duration_str)

        return table

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=threads)
    jobs = {}
    execution_order = []
    jobs_started_at = {}
    jobs_ended_at = {}
    exceptions = {}
    skipped = set()
    cache_path = pathlib.Path(".cache.pkl")
    cache = set() if fresh or not cache_path.exists() else pickle.loads(cache_path.read_bytes())
    tic = time.time()

    views_sp = "views" if len(cache) > 1 else "view"
    console_log(f"{len(cache):,d} {views_sp} already done")

    with rich.live.Live(display_progress(), vertical_overflow="visible") as live:
        dag.prepare()
        while dag.is_active():
            for view_key in dag.get_ready():

                # Check if the view_key can be skipped or not
                if view_key not in selected_view_keys:
                    dag.done(view_key)
                    continue
                execution_order.append(view_key)

                # A view can only be computed if all its dependencies have been computed
                # succesfully

                if any(dep_key in skipped or dep_key in exceptions for dep_key in map(client._table_reference_to_view_key, dag[view_key].dependencies)):
                    skipped.add(view_key)
                    dag.done(view_key)
                    continue

                # Submit a job, or print, or do nothing
                if dry or view_key in cache:
                    job = _do_nothing
                elif print_views:
                    job = functools.partial(
                        pretty_print_view,
                        view=dag[view_key].rename_table_references(table_reference_mapping=table_reference_mapping),
                        console=console
                    )
                else:
                    job = functools.partial(
                        client.materialize_view,
                        view=dag[view_key].rename_table_references(table_reference_mapping=table_reference_mapping)
                    )
                jobs[view_key] = executor.submit(job)
                jobs_started_at[view_key] = dt.datetime.now()

            # Check if any jobs are done. We notify the DAG by calling done when a job is done,
            # which will unlock the next views.
            for view_key in jobs_started_at:
                if view_key not in jobs_ended_at and jobs[view_key].done():
                    dag.done(view_key)
                    jobs_ended_at[view_key] = dt.datetime.now()
                    # Determine whether the job succeeded or not
                    if exception := jobs[view_key].exception():
                        raise exception
                        exceptions[view_key] = exception

            live.update(display_progress())

    # Save the cache
    all_done = not exceptions and not skipped
    cache = (
        set()
        if all_done
        else cache
        | {
            view_key
            for view_key in execution_order
            if view_key not in exceptions and view_key not in skipped
        }
    )
    if cache:
        cache_path.write_bytes(pickle.dumps(cache))
    else:
        cache_path.unlink(missing_ok=True)

    # Summary statistics
    if silent:
        return
    console.log(f"Took {round(time.time() - tic)}s")
    summary = rich.table.Table()
    summary.add_column("status")
    summary.add_column("count")
    if n := len(jobs_ended_at) - len(exceptions):
        summary.add_row(SUCCESS, f"{n:,d}")
    if n := len(exceptions):
        summary.add_row(ERRORED, f"{n:,d}")
    if n := len(skipped):
        summary.add_row(SKIPPED, f"{n:,d}")
    console.print(summary)

    # Summary of errors
    if exceptions:
        for view_key, exception in exceptions.items():
            console.print(str(dag[view_key]), style="bold red")
            console.print(exception)

        if fail_fast:
            raise Exception("Some views failed to build")
