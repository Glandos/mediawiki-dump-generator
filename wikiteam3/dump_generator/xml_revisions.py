import sys
import time
from urllib.parse import urlparse

import mwclient
import requests
from exceptions import PageMissingError
from log_error import logerror
from namespaces import Namespaces
from page_titles import read_titles
from page_xml import make_xml_from_page, make_xml_from_raw


def get_xml_revisions(config: dict, allpages=False, start=None):
    # FIXME: actually figure out the various strategies for each MediaWiki version
    apiurl = urlparse(config["api"])
    # FIXME: force the protocol we asked for! Or don't verify SSL if we asked HTTP?
    # https://github.com/WikiTeam/wikiteam/issues/358
    site = mwclient.Site(
        apiurl.netloc, apiurl.path.replace("api.php", ""), scheme=apiurl.scheme
    )

    if "all" not in config["namespaces"]:
        namespace_indices = config["namespaces"]
    else:
        namespace_indices = Namespaces(config).namespace_indices

    try:
        for namespace in namespace_indices:
            print("Trying to export all revisions from namespace %s" % namespace)
            # arvgeneratexml exists but was deprecated in 1.26 (while arv is from 1.27?!)
            arvparams = {
                "action": "query",
                "list": "allrevisions",
                "arvlimit": 50,
                "arvnamespace": namespace,
            }
            if not config["current-only"]:
                # We have to build the XML manually...
                # Skip flags, presumably needed to add <minor/> which is in the schema.
                # Also missing: parentid and contentformat.
                arvparams[
                    "arvprop"
                ] = "ids|timestamp|user|userid|size|sha1|contentmodel|comment|content"
                print(
                    "Trying to get wikitext from the allrevisions API and to build the XML"
                )
                while True:
                    try:
                        arv_request = site.api(
                            http_method=config["http_method"], **arvparams
                        )
                    except requests.exceptions.HTTPError as e:
                        if (
                            e.response.status_code == 405
                            and config["http_method"] == "POST"
                        ):
                            print("POST request to the API failed, retrying with GET")
                            config["http_method"] = "GET"
                            continue
                    except requests.exceptions.ReadTimeout as err:
                        # Hopefully temporary, just wait a bit and continue with the same request.
                        # No point putting a limit to retries, we'd need to abort everything.
                        # TODO: reuse the retry logic of the check_api phase? Or force mwclient
                        # to use the retry adapter we use for our own requests requests.Session()?
                        print("ERROR: %s" % str(err))
                        print("Sleeping for 20 seconds")
                        time.sleep(20)
                        continue

                    for page in arv_request["query"]["allrevisions"]:
                        yield make_xml_from_page(page)
                    if "continue" in arv_request:
                        arvparams["arvcontinue"] = arv_request["continue"][
                            "arvcontinue"
                        ]
                    else:
                        # End of continuation. We are done with this namespace.
                        break

            else:
                # FIXME: this is not current, just different strategy to do all revisions
                # Just cycle through revision IDs and use the XML as is
                print("Trying to list the revisions and to export them one by one")
                # We only need the revision ID, all the rest will come from the raw export
                arvparams["arvprop"] = "ids"
                try:
                    arv_request = site.api(
                        http_method=config["http_method"], **arvparams
                    )
                except requests.exceptions.HTTPError as e:
                    if (
                        e.response.status_code == 405
                        and config["http_method"] == "POST"
                    ):
                        print("POST request to the API failed, retrying with GET")
                        config["http_method"] = "GET"
                        continue
                exportparams = {
                    "action": "query",
                    "export": "1",
                }
                # Skip the namespace if it's empty
                if len(arv_request["query"]["allrevisions"]) < 1:
                    continue
                # Repeat the arv_request with new arvparams until done
                while True:
                    # Reset revision IDs from the previous batch from arv
                    revids = []
                    for page in arv_request["query"]["allrevisions"]:
                        for revision in page["revisions"]:
                            revids.append(str(revision["revid"]))
                    print(
                        "        %d more revisions listed, until %s"
                        % (len(revids), revids[-1])
                    )

                    # We can now get the XML for one revision at a time
                    # FIXME: we can actually get them in batches as we used to
                    # but need to figure out the continuation and avoid that the API
                    # chooses to give us only the latest for each page
                    for revid in revids:
                        exportparams["revids"] = revid
                        try:
                            export_request = site.api(
                                http_method=config["http_method"], **exportparams
                            )
                        except requests.exceptions.HTTPError as e:
                            if (
                                e.response.status_code == 405
                                and config["http_method"] == "POST"
                            ):
                                print(
                                    "POST request to the API failed, retrying with GET"
                                )
                                config["http_method"] = "GET"
                                export_request = site.api(
                                    http_method=config["http_method"], **exportparams
                                )

                        # This gives us a self-standing <mediawiki> element
                        # but we only need the inner <page>: we can live with
                        # duplication and non-ordering of page titles, but the
                        # repeated header is confusing and would not even be valid
                        xml = export_request["query"]["export"]["*"]
                        yield make_xml_from_raw(xml)

                    if "continue" in arv_request:
                        # Get the new ones
                        arvparams["arvcontinue"] = arv_request["continue"][
                            "arvcontinue"
                        ]
                        try:
                            arv_request = site.api(
                                http_method=config["http_method"], **arvparams
                            )
                        except requests.exceptions.HTTPError as e:
                            if (
                                e.response.status_code == 405
                                and config["http_method"] == "POST"
                            ):
                                print(
                                    "POST request to the API failed, retrying with GET"
                                )
                                config["http_method"] = "GET"
                                arv_request = site.api(
                                    http_method=config["http_method"], **arvparams
                                )
                        except requests.exceptions.ReadTimeout as err:
                            # As above
                            print("ERROR: %s" % str(err))
                            print("Sleeping for 20 seconds")
                            time.sleep(20)
                            # But avoid rewriting the same revisions
                            arv_request["query"]["allrevisions"] = []
                            continue
                    else:
                        # End of continuation. We are done with this namespace.
                        break

    except (KeyError, mwclient.errors.InvalidResponse) as e:
        print(e)
        # TODO: check whether the KeyError was really for a missing arv API
        print("Warning. Could not use allrevisions. Wiki too old?")
        if config["current-only"]:
            # The raw XML export in the API gets a title and gives the latest revision.
            # We could also use the allpages API as generator but let's be consistent.
            print("Getting titles to export the latest revision for each")
            count = 0
            for title in read_titles(config, start=start):
                # TODO: respect verbose flag, reuse output from get_xml_page
                print("    %s" % title)
                # TODO: as we're doing one page and revision at a time, we might
                # as well use xml format and exportnowrap=1 to use the string of,
                # XML as is, but need to check how well the library handles it.
                exportparams = {
                    "action": "query",
                    "titles": title,
                    "export": "1",
                }
                try:
                    export_request = site.api(
                        http_method=config["http_method"], **exportparams
                    )
                except requests.exceptions.HTTPError as e:
                    if (
                        e.response.status_code == 405
                        and config["http_method"] == "POST"
                    ):
                        print("POST request to the API failed, retrying with GET")
                        config["http_method"] = "GET"
                        export_request = site.api(
                            http_method=config["http_method"], **exportparams
                        )

                xml = export_request["query"]["export"]["*"]
                count += 1
                if count % 10 == 0:
                    print("")
                    print("->  Downloaded %d pages" % count)
                # Because we got the fancy XML from the JSON format, clean it:
                yield make_xml_from_raw(xml)
        else:
            # This is the closest to what we usually do with Special:Export:
            # take one title at a time and try to get all revisions exported.
            # It differs from the allrevisions method because it actually needs
            # to be input the page titles; otherwise, the requests are similar.
            # The XML needs to be made manually because the export=1 option
            # refuses to return an arbitrary number of revisions (see above).
            print("Getting titles to export all the revisions of each")
            count = 0
            titlelist = []
            # TODO: Decide a suitable number of a batched request. Careful:
            # batched responses may not return all revisions.
            for titlelist in read_titles(config, start=start, batch=False):
                if type(titlelist) is not list:
                    titlelist = [titlelist]
                for title in titlelist:
                    print("    %s" % title)
                # Try and ask everything. At least on MediaWiki 1.16, uknown props are discarded:
                # "warnings":{"revisions":{"*":"Unrecognized values for parameter 'rvprop': userid, sha1, contentmodel"}}}
                pparams = {
                    "action": "query",
                    "titles": "|".join(titlelist),
                    "prop": "revisions",
                    # 'rvlimit': 50,
                    "rvprop": "ids|timestamp|user|userid|size|sha1|contentmodel|comment|content",
                }
                try:
                    prequest = site.api(http_method=config["http_method"], **pparams)
                except requests.exceptions.HTTPError as e:
                    if (
                        e.response.status_code == 405
                        and config["http_method"] == "POST"
                    ):
                        print("POST request to the API failed, retrying with GET")
                        config["http_method"] = "GET"
                        export_request = site.api(
                            http_method=config["http_method"], **exportparams
                        )
                except mwclient.errors.InvalidResponse:
                    logerror(
                        config,
                        text="Error: page inaccessible? Could not export page: %s"
                        % ("; ".join(titlelist)),
                    )
                    continue

                # Be ready to iterate if there is continuation.
                while True:
                    # Get the revision data returned by the API: prequest is the initial request
                    # or the new one after continuation at the bottom of this while loop.
                    # The array is called "pages" even if there's only one.
                    try:
                        pages = prequest["query"]["pages"]
                    except KeyError:
                        logerror(
                            config,
                            text="Error: page inaccessible? Could not export page: %s"
                            % ("; ".join(titlelist)),
                        )
                        break
                    # Go through the data we got to build the XML.
                    for pageid in pages:
                        try:
                            xml = make_xml_from_page(pages[pageid])
                            yield xml
                        except PageMissingError:
                            logerror(
                                config,
                                text="Error: empty revision from API. Could not export page: %s"
                                % ("; ".join(titlelist)),
                            )
                            continue

                    # Get next batch of revisions if there's more.
                    if "continue" in prequest.keys():
                        print("Getting more revisions for the page")
                        for key, value in prequest["continue"]:
                            pparams[key] = value
                    elif "query-continue" in prequest.keys():
                        rvstartid = prequest["query-continue"]["revisions"]["rvstartid"]
                        pparams["rvstartid"] = rvstartid
                    else:
                        break

                    try:
                        prequest = site.api(
                            http_method=config["http_method"], **pparams
                        )
                    except requests.exceptions.HTTPError as e:
                        if (
                            e.response.status_code == 405
                            and config["http_method"] == "POST"
                        ):
                            print("POST request to the API failed, retrying with GET")
                            config["http_method"] = "GET"
                            prequest = site.api(
                                http_method=config["http_method"], **pparams
                            )

                # We're done iterating for this title or titles.
                count += len(titlelist)
                # Reset for the next batch.
                titlelist = []
                if count % 10 == 0:
                    print("")
                    print("-> Downloaded %d pages" % count)

    except mwclient.errors.MwClientError as e:
        print(e)
        print("This mwclient version seems not to work for us. Exiting.")
        sys.exit()
