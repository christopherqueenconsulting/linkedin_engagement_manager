#  Copyright (c) 2024. Christopher Queen Consulting LLC (http://www.ChristopherQueenConsulting.com/)
import base64
import os
import tempfile
import zipfile
from random import randint
from typing import Any, Tuple

import mammoth
import streamlit as st
import streamlit_ext as ste

#from docx import Document
from markdownify import markdownify as md
from openai import OpenAI
from streamlit.runtime.uploaded_file_manager import UploadedFile

#from cqc_lem.streamlit import UploadedFile
from cqc_lem.utilities.mime_type_helper import get_file_mime_type

CODE_LANGUAGES = [
    "abap", "abnf", "actionscript", "ada", "agda", "al", "antlr4", "apacheconf",
    "apex", "apl", "applescript", "aql", "arduino", "arff", "asciidoc", "asm6502",
    "asmatmel", "aspnet", "autohotkey", "autoit", "avisynth", "avroIdl", "bash",
    "basic", "batch", "bbcode", "bicep", "birb", "bison", "bnf", "brainfuck",
    "brightscript", "bro", "bsl", "c", "cfscript", "chaiscript", "cil", "clike",
    "clojure", "cmake", "cobol", "coffeescript", "concurnas", "coq", "cpp", "crystal",
    "csharp", "cshtml", "csp", "cssExtras", "css", "csv", "cypher", "d", "dart",
    "dataweave", "dax", "dhall", "diff", "django", "dnsZoneFile", "docker", "dot",
    "ebnf", "editorconfig", "eiffel", "ejs", "elixir", "elm", "erb", "erlang",
    "etlua", "excelFormula", "factor", "falselang", "firestoreSecurityRules", "flow",
    "fortran", "fsharp", "ftl", "gap", "gcode", "gdscript", "gedcom", "gherkin",
    "git", "glsl", "gml", "gn", "goModule", "go", "graphql", "groovy", "haml",
    "handlebars", "haskell", "haxe", "hcl", "hlsl", "hoon", "hpkp", "hsts", "http",
    "ichigojam", "icon", "icuMessageFormat", "idris", "iecst", "ignore", "inform7",
    "ini", "io", "j", "java", "javadoc", "javadoclike", "javascript", "javastacktrace",
    "jexl", "jolie", "jq", "jsExtras", "jsTemplates", "jsdoc", "json", "json5", "jsonp",
    "jsstacktrace", "jsx", "julia", "keepalived", "keyman", "kotlin", "kumir", "kusto",
    "latex", "latte", "less", "lilypond", "liquid", "lisp", "livescript", "llvm", "log",
    "lolcode", "lua", "magma", "makefile", "markdown", "markupTemplating", "markup",
    "matlab", "maxscript", "mel", "mermaid", "mizar", "mongodb", "monkey", "moonscript",
    "n1ql", "n4js", "nand2tetrisHdl", "naniscript", "nasm", "neon", "nevod", "nginx",
    "nim", "nix", "nsis", "objectivec", "ocaml", "opencl", "openqasm", "oz", "parigp",
    "parser", "pascal", "pascaligo", "pcaxis", "peoplecode", "perl", "phpExtras", "php",
    "phpdoc", "plsql", "powerquery", "powershell", "processing", "prolog", "promql",
    "properties", "protobuf", "psl", "pug", "puppet", "pure", "purebasic", "purescript",
    "python", "q", "qml", "qore", "qsharp", "r", "racket", "reason", "regex", "rego",
    "renpy", "rest", "rip", "roboconf", "robotframework", "ruby", "rust", "sas", "sass",
    "scala", "scheme", "scss", "shellSession", "smali", "smalltalk", "smarty", "sml",
    "solidity", "solutionFile", "soy", "sparql", "splunkSpl", "sqf", "sql", "squirrel",
    "stan", "stylus", "swift", "systemd", "t4Cs", "t4Templating", "t4Vb", "tap", "tcl",
    "textile", "toml", "tremor", "tsx", "tt2", "turtle", "twig", "typescript", "typoscript",
    "unrealscript", "uorazor", "uri", "v", "vala", "vbnet", "velocity", "verilog", "vhdl",
    "vim", "visualBasic", "warpscript", "wasm", "webIdl", "wiki", "wolfram", "wren", "xeora",
    "xmlDoc", "xojo", "xquery", "yaml", "yang", "zig"
]




@st.cache_data
def get_custom_css():
    # Embed custom fonts using HTML and CSS
    css = """
        <style>
            @font-face {
                font-family: "Franklin Gothic";
                src: url("https://db.onlinewebfonts.com/t/9c9dbb999dd7068f51335d93cc7328bd.eot");
                src: url("https://db.onlinewebfonts.com/t/9c9dbb999dd7068f51335d93cc7328bd.eot?#iefix")format("embedded-opentype"),
                url("https://db.onlinewebfonts.com/t/9c9dbb999dd7068f51335d93cc7328bd.woff2")format("woff2"),
                url("https://db.onlinewebfonts.com/t/9c9dbb999dd7068f51335d93cc7328bd.woff")format("woff"),
                url("https://db.onlinewebfonts.com/t/9c9dbb999dd7068f51335d93cc7328bd.ttf")format("truetype"),
                url("https://db.onlinewebfonts.com/t/9c9dbb999dd7068f51335d93cc7328bd.svg#Franklin Gothic")format("svg");
            }

            @font-face {
                font-family: 'ITC New Baskerville';
                src: url("https://db.onlinewebfonts.com/t/501ade6e29baa5c62c15ec28f3ed2c62.eot");
                src: url("https://db.onlinewebfonts.com/t/501ade6e29baa5c62c15ec28f3ed2c62.eot?#iefix")format("embedded-opentype"),
                url("https://db.onlinewebfonts.com/t/501ade6e29baa5c62c15ec28f3ed2c62.woff2")format("woff2"),
                url("https://db.onlinewebfonts.com/t/501ade6e29baa5c62c15ec28f3ed2c62.woff")format("woff"),
                url("https://db.onlinewebfonts.com/t/501ade6e29baa5c62c15ec28f3ed2c62.ttf")format("truetype"),
                url("https://db.onlinewebfonts.com/t/501ade6e29baa5c62c15ec28f3ed2c62.svg#ITC New Baskerville")format("svg");
            }

            body {
                font-family: 'Franklin Gothic', sans-serif;
            }

            h1, h2, h3, h4, h5, h6 {
                font-family: 'Franklin Gothic', sans-serif;
                font-weight: normal;
            }

            p {
                font-family: 'ITC New Baskerville', sans-serif;
                font-weight: normal;
            }
        </style>
        """
    return css


@st.cache_resource(hash_funcs={OpenAI: id})
def get_openai_client_instance(temperature: float, model: str) -> OpenAI:
    client = OpenAI(
        # This is the default and can be omitted
        # api_key=os.environ.get("OPENAI_API_KEY"),
    )

    """
    This function returns a cached instance of ChatOpenAI based on the temperature and model.
    If the temperature or model changes, a new instance will be created and cached.
    """
    return client


def get_file_extension_from_filepath(file_path: str, remove_leading_dot: bool = False) -> str:
    basename = os.path.basename(file_path)
    file_name, file_extension = os.path.splitext(basename)
    if remove_leading_dot and file_extension.startswith("."):
        # st.info("Removing leading dot from file extension: " + file_extension)
        file_extension = file_extension[1:]

    if file_extension:
        file_extension = file_extension.lower()

    # st.info("Base Name: " + basename + " | File Name: " + file_name + " | File Extension : " + file_extension)

    return file_extension


def get_language_from_file_path(file_path):
    # Extract file extension from the file path
    file_extension = get_file_extension_from_filepath(file_path, True)

    # Check if the file extension exists in the mapping
    if file_extension in CODE_LANGUAGES:
        # st.info(file_extension + " | Found in CODE_LANGUAGES")
        return file_extension
    else:
        # st.info(file_extension + " | NOT Found in CODE_LANGUAGES")
        return None  # Return None if the file extension is not found


def define_code_language_selection(unique_key: str | int, default_option: str = 'java'):
    # List of available languages

    selected_language = st.selectbox(label="Select Code Language",
                                     key="language_select_" + unique_key,
                                     options=CODE_LANGUAGES,
                                     index=CODE_LANGUAGES.index(default_option))
    return selected_language


def define_chatGPTModel(unique_key: str | int, default_min_value: float = .2, default_max_value: float = .8,
                        default_temp_value: float = .2,
                        default_step: float = 0.1, default_option="gpt-4o") -> Tuple[str, float]:
    # Dropdown for selecting ChatGPT models
    model_options = [default_option, "gpt-4-turbo", "gpt-4-1106-preview", "gpt-3.5-turbo", "gpt-3.5-turbo-16k-0613"]
    selected_model = st.selectbox(label="Select ChatGPT Model",
                                  key="chat_select_" + unique_key,
                                  options=model_options,
                                  index=model_options.index(default_option))

    # Slider for selecting a value (ranged from 0.2 to 0.8, with step size 0.01)
    # Define the ranges and corresponding labels
    ranges = [(0, 0.3, "Low temperature: More focused, coherent, and conservative outputs."),
              (0.3, 0.7, "Medium temperature: Balanced creativity and coherence."),
              (0.7, 1, "High temperature: Highly creative and diverse, but potentially less coherent.")]

    temperature = st.slider(label="Chat GPT Temperature",
                            key="chat_temp_" + unique_key,
                            min_value=max(default_min_value, 0),
                            max_value=min(default_max_value, 1),
                            step=default_step, value=default_temp_value,
                            format="%.2f")

    # Determine the label based on the selected value
    for low, high, label in ranges:
        if low <= temperature <= high:
            st.write(label)
            break

    return selected_model, temperature


def reset_session_key_value(key: str):
    st.session_state[key] = str(randint(1000, 100000000))


def add_upload_file_element(uploader_text: str, accepted_file_types: list[str], success_message: bool = True,
                            accept_multiple_files: bool = False) -> list[tuple[Any, str]] | tuple[Any, str] | tuple[
    None, None]:
    # Button to reset the multi file uploader
    reset_label = "Reset " + uploader_text + " File Uploader"
    reset_key = reset_label.replace(" ", "_")

    if reset_key not in st.session_state:
        reset_session_key_value(reset_key)

    uploaded_files = st.file_uploader(label=uploader_text, type=accepted_file_types,
                                      accept_multiple_files=accept_multiple_files, key=st.session_state[reset_key])

    if accept_multiple_files:
        if st.button("Remove All Files", key="Checkbox_" + st.session_state[reset_key]):
            reset_session_key_value(reset_key)
            st.rerun()

        uploaded_file_paths = []
        for uploaded_file in uploaded_files:
            if uploaded_file is not None:
                # Get the original file name
                original_file_name = uploaded_file.name

                # Create a temporary file to store the uploaded file
                temp_file_name = upload_file_to_temp_path(uploaded_file)

                uploaded_file_paths.append((original_file_name, temp_file_name))
        if uploaded_files and success_message:
            st.success("File(s) uploaded successfully.")
        return uploaded_file_paths

    elif uploaded_files is not None:
        # Get the original file name
        original_file_name = uploaded_files.name
        # Create a temporary file to store the uploaded file
        temp_file_name = upload_file_to_temp_path(uploaded_files)

        if success_message:
            st.success("File uploaded successfully.")
        return original_file_name, temp_file_name
    else:
        return None, None


def upload_file_to_temp_path(uploaded_file: UploadedFile):
    file_extension = get_file_extension_from_filepath(uploaded_file.name)

    # Create a temporary file to store the uploaded instructions
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=file_extension)
    temp_file.write(uploaded_file.getvalue())
    # temp_file.close()

    return temp_file.name


def process_file(file_path, allowed_file_extensions):
    """Using a file path determine if the file is a zip or single file and gives the contents back if single or dict mapping the studnet name and timestamp back to the combined contents"""
    # If it's a zip file
    if file_path.endswith('.zip'):
        with zipfile.ZipFile(file_path, 'r') as zip_file:
            folder_contents = {}
            for zip_info in zip_file.infolist():
                if any(zip_info.filename.lower().endswith(ext) for ext in allowed_file_extensions):
                    folder_path = os.path.dirname(zip_info.filename)
                    with zip_file.open(zip_info) as file:
                        file_contents = file.read()
                    folder_contents.setdefault(folder_path, []).append(file_contents)

            for folder_path, files in folder_contents.items():
                concatenated_contents = b''.join(files)
                print(f"Contents of folder '{folder_path}': {concatenated_contents.decode()}")

    # If it's a single file
    else:
        if any(file_path.lower().endswith(ext) for ext in allowed_file_extensions):
            with open(file_path, 'r') as file:
                print("Contents of single file:", file.read())


def on_download_click(file_path: str, button_label: str, download_file_name: str) -> str:
    file_extension = get_file_extension_from_filepath(download_file_name)
    mime_type = get_file_mime_type(file_extension)
    # st.info("file_extension: " + file_extension + " | mime_type: " + mime_type)

    # file_content = read_file(file_path)
    # Read the content of the file
    with open(file_path, "rb") as file:
        file_content = file.read()

    # st.info("file_path: "+file_path+" | download_file_name: "+download_file_name)
    # st.markdown(file_content)

    # Trigger the download of the file
    return ste.download_button(label=button_label, data=file_content,
                               file_name=download_file_name, mime=mime_type
                               # , key=download_file_name
                               )


def create_zip_file(file_paths: list[tuple[str, str]]) -> str:
    # Create a temporary file to store the zip file
    zip_file = tempfile.NamedTemporaryFile(delete=False)
    zip_file.close()  # Close the file to use it as the output path for the zip file

    with zipfile.ZipFile(zip_file.name, 'w') as zipf:
        for orig_file_path, temp_file_path in file_paths:
            # Get the base file name from the original file path
            base_file_name = os.path.basename(orig_file_path)
            # Add the temporary file to the zip file with the original file name
            zipf.write(temp_file_path, arcname=base_file_name)

    # Return the path of the zip file
    return zip_file.name


def prefix_content_file_name(filename: str, content: str):
    return "# File: " + filename + "\n\n" + content


@st.cache_data
def convert_content_to_markdown(content: str) -> str:
    return md(content)


"""
def convert_tables_to_json_in_tmp__file(doc: Document) -> str:
    for table in doc.tables:
        data = [[cell.text for cell in row.cells] for row in table.rows]
        df = pd.DataFrame(data)

        # Remove the table
        t = table._element
        parent = t.getparent()
        parent.remove(t)

        # Add new json string to the parent in its place
        doc.add_paragraph(df.to_json(orient="records"))

        # Clear the table reference
        t._t = t._element = None

    # Save to temp file
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.docx')
    doc.save(temp_file.name)

    return temp_file.name
"""

@st.cache_data
def read_file(file_path: str, convert_to_markdown: bool = False) -> str:
    """Return the file contents in string format. If file ends in .docx will convert it to json and return"""
    file_name, file_extension = os.path.splitext(file_path)

    if convert_to_markdown:
        with open(file_path, mode='rb') as f:
            results = mammoth.convert_to_html(f)
            contents = convert_content_to_markdown(results.value)
    else:
        with open(file_path, mode='r') as f:
            contents = f.read()

    return str(contents)


def get_file_as_data_image(file_path: str):
    file_ = open(file_path, "rb")
    contents = file_.read()
    data_url = base64.b64encode(contents).decode("utf-8")
    file_.close()
    return "data:image/png;base64," + data_url
