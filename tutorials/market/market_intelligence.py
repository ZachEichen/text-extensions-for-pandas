# market_intelligence.py

# Python code related to the market intelligence use case blog series.


from typing import *
import pandas as pd
import text_extensions_for_pandas as tp
import ibm_watson
import ibm_watson.natural_language_understanding_v1 as nlu
import ibm_cloud_sdk_core
import spacy
import time
import urllib.request
import os
import regex
import threading


def download_article(url: str):
    req = urllib.request.Request(url + "?printable", 
                                 headers={"User-Agent": "DefinitelyNotABot/3.0"})
    html = urllib.request.urlopen(req).read().decode("utf-8")
    return html

def maybe_download_articles() -> pd.DataFrame:
    file_name = "ibm_press_releases.feather"
    if not os.path.exists(file_name):
        print("No cached documents; downloading them.")
        with open("ibm_press_releases.txt", "r") as f:
            lines = [l.strip() for l in f.readlines()]
            article_urls = [l for l in lines if len(l) > 0 and l[0] != "#"]

        article_htmls = [
            download_article(url) for url in article_urls
        ]
        to_write = pd.DataFrame({"url": article_urls, 
                                 "html": article_htmls})
        to_write.to_feather(file_name)
    return pd.read_feather(file_name)


def extract_named_entities_and_semantic_roles(doc_html: str, nlu_api) -> Dict:
    return call_nlu_with_retry(doc_html, nlu_api, True, True)

def extract_named_entities(doc_html: str, nlu_api) -> Dict:
    return call_nlu_with_retry(doc_html, nlu_api, True, False)

def extract_semantic_roles(doc_html: str, nlu_api) -> Dict:
    return call_nlu_with_retry(doc_html, nlu_api, False, True)

def identify_persons_quoted_by_name(named_entities_result,
                                    semantic_roles_result = None) -> pd.DataFrame:
    """
    The second phase of processing from the first part of this series, rolled into 
    a single function.
    
    :param named_entities_result: Response object from invoking Watson Natural Language
     Understanding's named entity model on the document
     
    :param semantic_roles_result: Response object from invoking Watson Natural Language
     Understanding's semantic roles model on the document, or None if the results of
     the semantic roles model are inside `named_entities_result`
    
    :returns: A Pandas DataFrame containing information about potential executives
     that the document quoted by name
    """
    
    # Convert the output of Watson Natural Language Understanding to DataFrames.
    dfs = tp.io.watson.nlu.parse_response(named_entities_result)
    entity_mentions_df = dfs["entity_mentions"]
    
    srl_dfs = (tp.io.watson.nlu.parse_response(semantic_roles_result)
               if semantic_roles_result is not None else dfs)
    semantic_roles_df = srl_dfs["semantic_roles"]
    
    # Extract mentions of person names and company names
    person_mentions_df = entity_mentions_df[entity_mentions_df["type"] == "Person"]
    
    # Extract instances of subjects that made statements
    quotes_df = semantic_roles_df[semantic_roles_df["action.normalized"] == "say"]
    subjects_df = quotes_df[["subject.text"]].copy().reset_index(drop=True)
    
    # Identify the locations of subjects within the document.
    doc_text = entity_mentions_df["span"].array.document_text
    
    # Use String.index() to find where the strings in "subject.text" begin
    begins = [doc_text.index(s) for s in subjects_df["subject.text"]]
    subjects_df["begin"] = pd.Series(begins, dtype=int)
    subjects_df["end"] = subjects_df["begin"] + subjects_df["subject.text"].str.len()
    subjects_df["span"] = tp.SpanArray(doc_text, subjects_df["begin"], subjects_df["end"])
    
    # Align subjects with person names
    execs_df = tp.spanner.contain_join(subjects_df["span"], 
                                       person_mentions_df["span"],
                                       "subject", "person")
    return execs_df

paragraph_break_re = regex.compile(r"\n+")

def find_paragraph_spans(doc_text: str) -> tp.SpanArray:
    """
    Subroutine of perform_targeted_dependency_parsing that we introduce 
    in the third part of the series. Splits document text into paragraphs
    and returns a SpanArray containing one span per paragraph.
    """
    # Find paragraph boundaries
    break_locs = [(a.start(), a.end()) 
                  for a in regex.finditer(paragraph_break_re, doc_text)]
    boundaries = break_locs + [(len(doc_text), len(doc_text))]
    
    # Split the document on paragraph boundaries
    begins = []
    ends = []
    begin = 0
    for b in boundaries:
        end = b[0]
        if end > begin:  # Ignore zero-length paragraphs
            begins.append(begin)
            ends.append(end)
        begin = b[1]
    return tp.SpanArray(doc_text, begins, ends)

def perform_targeted_dependency_parsing(
        spans_to_cover: Union[tp.SpanArray, pd.Series],
        language_model: spacy.language.Language) -> pd.DataFrame:  
    """
    Optimized version of `perform_dependency_parsing` that we introduce in the
    third part of the series.
    
    Identifies regions of the document to parse, then parses a those regions
    using SpaCy's depdendency parser, then converts the outputs of the parser 
    into a Pandas DataFrame of spans over the original document using Text 
    Extensions for Pandas.
    """
    spans_to_cover = tp.SpanArray.make_array(spans_to_cover)
    
    # Special case: No spans. Return empty DataFrame with correct schema.
    if len(spans_to_cover) == 0:
        return pd.DataFrame({
            "id": pd.Series([], dtype=int),
            "span": pd.Series([], dtype=tp.SpanDtype()),
            "tag": pd.Series([], dtype=str),
            "dep": pd.Series([], dtype=str),
            "head": pd.Series([], dtype=int),
        })
        return tp.io.spacy.make_tokens_and_features(
            "", language_model
            )[["id", "span", "tag", "dep", "head"]]
    
    doc_text = spans_to_cover.document_text
    all_paragraphs = find_paragraph_spans(doc_text)
    covered_paragraphs = tp.spanner.contain_join(pd.Series(all_paragraphs), 
                                                 pd.Series(spans_to_cover),
                                                "paragraph", "span")["paragraph"].array
    
    
    offset = 0
    to_stack = []
    for paragraph_span in covered_paragraphs:
        # Tokenize and parse the paragraph
        paragraph_text = paragraph_span.covered_text
        paragraph_tokens = tp.io.spacy.make_tokens_and_features(
            paragraph_text, language_model
            )[["id", "span", "tag", "dep", "head"]]
        
        # Convert token spans to original document text
        span_array_before = paragraph_tokens["span"].array
        paragraph_tokens["span"] = \
            tp.SpanArray(paragraph_span.target_text,
                         paragraph_span.begin + span_array_before.begin,
                         paragraph_span.begin + span_array_before.end)
        
        # Adjust token IDs
        paragraph_tokens["id"] += offset
        paragraph_tokens["head"] += offset
        paragraph_tokens.index += offset
        
        to_stack.append(paragraph_tokens)
        offset += len(paragraph_tokens.index)
    return pd.concat(to_stack)

def perform_dependency_parsing(doc_text: str, spacy_language_model):
    """
    First phase of processing from the second part of the series.
    
    Parses a document using SpaCy's depdendency parser, then converts the
    outputs of the parser into a Pandas DataFrame using Text Extensions for Pandas.
    """
    return (
        tp.io.spacy.make_tokens_and_features(doc_text, spacy_language_model)
        [["id", "span", "tag", "dep", "head"]])

def extract_titles_of_persons(persons_quoted: pd.DataFrame, parse_features: pd.DataFrame) -> pd.DataFrame:
    """
    Second phase of processing from the second part of the series.
    
    :param persons_quoted: DataFrame of persons quoted in the target document, as
     returned by :func:`identify_persons_quoted_by_name`.
    :param parse_features: Dependency parse of the document, as returned by 
     :func:`perform_dependency_parsing`.
    """
    # Identify the portion of the parse that aligns with persons_quoted["subject"]
    phrase_tokens = (
        tp.spanner.contain_join(persons_quoted["subject"], parse_features["span"], 
                                "subject", "span")
        .merge(parse_features)
        .set_index("id", drop=False)
    )
    nodes = phrase_tokens[["id", "span", "tag", "subject"]].reset_index(drop=True)
    edges = phrase_tokens[["id", "head", "dep"]].reset_index(drop=True)

    # Identify the portion of the parse that aligns with persons_quoted["person"]
    person_nodes = (
        tp.spanner.overlap_join(persons_quoted["person"], nodes["span"],
                                "person", "span")
        .merge(nodes)
    )
    
    # Identify the edges we want to traverse from the person nodes
    filtered_edges = edges[edges["dep"].isin(["appos", "compound"])]
    
    # Transitive closure starting from our seed set of nodes and traversing 
    # the selected set of edges.
    selected_nodes = person_nodes.drop(columns="person").copy()
    previous_num_nodes = 0

    # Keep going as long as the previous round added nodes to our set.
    while len(selected_nodes.index) > previous_num_nodes:
        previous_num_nodes = len(selected_nodes.index)

        # Traverse one edge out from all nodes in `selected_nodes`
        addl_nodes = (
            selected_nodes[["id"]]
            .merge(filtered_edges, left_on="id", right_on="head", suffixes=["_head", ""])[["id"]]
            .merge(nodes)
        )

        # Add any previously unselected node to `selected_nodes`
        selected_nodes = pd.concat([selected_nodes, addl_nodes]).drop_duplicates()
    
    # Take the parse tree nodes we added to the seed set and generate spans of titles
    title_nodes = selected_nodes[~selected_nodes["id"].isin(person_nodes["id"])]
    titles_df = (
        title_nodes
        .groupby("subject")
        .aggregate({"span": "sum"})
        .reset_index()
        .rename(columns={"span": "title"})
    )
    titles_df["subject"] = titles_df["subject"].astype(tp.SpanDtype())
    
    execs_with_titles_df = pd.merge(persons_quoted, titles_df)
    return execs_with_titles_df

# def call_nlu(doc_html: str, 
#              natural_language_understanding: 
#                  ibm_watson.NaturalLanguageUnderstandingV1) -> Any:
#     """
#     Pass a document through Natural Language Understanding, performing the 
#     analyses we need for the current use case.
    
#     :param doc_html: HTML contents of the web page
#     :param nlu: Preinitialized instance of the NLU Python API
#     :returns: Python object encapsulating the parsed JSON response from the web service.
#     """
#     return natural_language_understanding.analyze(
#                 html=doc_html,
#                 return_analyzed_text=True,
#                 features=nlu.Features(
#                     entities=nlu.EntitiesOptions(mentions=True),
#                     semantic_roles=nlu.SemanticRolesOptions()
#                 )).get_result()


def call_nlu_with_retry(
    doc_html: str, 
    natural_language_understanding: ibm_watson.NaturalLanguageUnderstandingV1,
    extract_entities: bool,
    extract_semantic_roles: bool) -> Any:
    """
    Pass a document through Natural Language Understanding, performing the 
    analyses we need for the current use case.
    
    Also handles retrying with exponential backoff.
    
    :param doc_html: HTML contents of the web page
    :param nlu: Preinitialized instance of the NLU Python API
    :returns: Python object encapsulating the parsed JSON response from the web service.
    """
    if extract_entities and extract_semantic_roles:
        nlu_features=nlu.Features(
                    entities=nlu.EntitiesOptions(mentions=True),
                    semantic_roles=nlu.SemanticRolesOptions())
    elif extract_entities and not extract_semantic_roles:
        nlu_features=nlu.Features(
                    entities=nlu.EntitiesOptions(mentions=True))
    elif not extract_entities and extract_semantic_roles:
        nlu_features=nlu.Features(
                    semantic_roles=nlu.SemanticRolesOptions())
    else:
        raise ValueError("Must run at least one NLU model.")
    
    num_tries = 0
    MAX_RETRIES = 8
    RATE_LIMIT_ERROR_CODE = 429
    last_exception = None
    while num_tries < MAX_RETRIES:
        num_tries += 1
        try:
            return natural_language_understanding.analyze(
                html=doc_html,
                return_analyzed_text=True,
                features=nlu_features).get_result()
        except ibm_cloud_sdk_core.api_exception.ApiException as e:
            # Retry logic in case we hit the rate limit
            if e.code != RATE_LIMIT_ERROR_CODE:
                raise e
            sleep_time = 2 ** (num_tries - 1)
            print(f"Request failed {num_tries} times; retrying in {sleep_time} sec")
            time.sleep(sleep_time)

    raise Exception(f"Exceeded limit of {MAX_RETRIES} retries.")



# def extract_execs(doc_html: str, api_key: str, service_url: str) -> pd.DataFrame:
#     """
#     Part 1 of the blog post as a single function.
    
#     :param doc_html: HTML contents of the target document
#     :api_key: API key for accessing Watson Natural Language Understanding
#     :service_url: Web service URL for accessing Watson Natural Language Understanding
    
#     :returns: DataFrame containing information about executives mentioned in the 
#      target document. The DataFrame will have two columns:
#     """
#     # Feed the press release through Watson Natural Language Understanding
#     natural_language_understanding = ibm_watson.NaturalLanguageUnderstandingV1(
#             version="2021-01-01", 
#             authenticator=ibm_cloud_sdk_core.authenticators.IAMAuthenticator(api_key))
#     natural_language_understanding.set_service_url(service_url)
#     nlu_response = call_nlu(doc_html, natural_language_understanding)
#     return nlu_result_to_execs(nlu_response)


# def add_titles(execs_df: pd.DataFrame, parse_features: pd.DataFrame) -> pd.DataFrame:
#     """
#     Part 2 of the blog post as a single function.
    
#     :param execs_df: DataFrame of executives mentioned in the target document, as
#      returned by :func:`extract_execs`.
#     :param parse_features: Dependency parse of the document, as returned by a SpaCy
#      language model and converted to a DataFrame by 
#      :func:`tp.io.spacy.make_tokens_and_features`
#     """
#     # Identify the portion of the parse that aligns with execs_df["subject"]
#     phrase_tokens = (
#         tp.spanner.contain_join(execs_df["subject"], parse_features["span"], 
#                                 "subject", "span")
#         .merge(parse_features)
#         .set_index("id", drop=False)
#     )
#     nodes = phrase_tokens[["id", "span", "tag", "subject"]].reset_index(drop=True)
#     edges = phrase_tokens[["id", "head", "dep"]].reset_index(drop=True)

#     # Identify the portion of the parse that aligns with execs_df["person"]
#     person_nodes = (
#         tp.spanner.overlap_join(execs_df["person"], nodes["span"],
#                                 "person", "span")
#         .merge(nodes)
#     )
    
#     # Identify the edges we want to traverse from the person nodes
#     filtered_edges = edges[edges["dep"].isin(["appos", "compound"])]
    
#     # Transitive closure starting from our seed set of nodes and traversing 
#     # the selected set of edges.
#     selected_nodes = person_nodes.drop(columns="person").copy()
#     previous_num_nodes = 0

#     # Keep going as long as the previous round added nodes to our set.
#     while len(selected_nodes.index) > previous_num_nodes:
#         previous_num_nodes = len(selected_nodes.index)

#         # Traverse one edge out from all nodes in `selected_nodes`
#         addl_nodes = (
#             selected_nodes[["id"]]
#             .merge(filtered_edges, left_on="id", right_on="head", suffixes=["_head", ""])[["id"]]
#             .merge(nodes)
#         )

#         # Add any previously unselected node to `selected_nodes`
#         selected_nodes = pd.concat([selected_nodes, addl_nodes]).drop_duplicates()
    
#     # Take the parse tree nodes we added to the seed set and generate spans of titles
#     title_nodes = selected_nodes[~selected_nodes["id"].isin(person_nodes["id"])]
#     titles_df = (
#         title_nodes
#         .groupby("subject")
#         .aggregate({"span": "sum"})
#         .reset_index()
#         .rename(columns={"span": "title"})
#     )
#     titles_df["subject"] = titles_df["subject"].astype(tp.SpanDtype())
    
#     execs_with_titles_df = pd.merge(execs_df, titles_df)
#     return execs_with_titles_df

# def merge_dataframes(url_to_df: Dict[str, pd.DataFrame]) -> pd.DataFrame:
#     """
#     :param: url_to_df: Mapping from document URL to a DataFrame of extraction results for that document
#     """
#     to_stack = []
#     for url, df in url_to_df.items():
#         if len(df.index) > 0:
#             to_stack.append(pd.DataFrame({
#                 "url": url,
#                 # Drop span location information and pass through strings
#                 "subject": df["subject"].array.covered_text,
#                 "person": df["person"].array.covered_text,
#                 "title": df["title"].array.covered_text,
#             }))
#     return pd.concat(to_stack)


from abc import ABC, abstractmethod

class RateLimitedActor(ABC):
    """
    Abstract base class for rate-limited actors.
    """
    def __init__(self, requests_per_sec: float):
        self._sec_per_request = 1.0 / requests_per_sec
        #self.options(max_concurrency=requests_per_sec)
        self._start_time = time.time()
        self._last_request_time = self._start_time - self._sec_per_request
        self._last_request_time_lock = threading.Lock()
        
    def process(self, value: Any) -> Any:
        """"""
        # Basic rate-limiting logic
        while True:
            with self._last_request_time_lock:
                time_since_request = time.time() - self._last_request_time
                if time_since_request >= self._sec_per_request:
                    self._last_request_time = time.time()
                    #print(f"Making a request at T={self._last_request_time - self._start_time:.4f} sec")
                    break
            time_until_deadline = self._sec_per_request - time_since_request
            #print(f"Sleeping {time_until_deadline} sec to enforce rate limit")
            time.sleep(time_until_deadline)
        return self.process_internal(value)
    
    @abstractmethod
    def process_internal(self, value: Any) -> Any:
        raise NotImplementedError("Subclasses must implement this method")
