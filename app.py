import spacy
import re
from urllib.parse import unquote, quote, urlparse
import urllib.parse
from flask import Flask, jsonify, request, render_template
import requests
import sqlite3
import rdflib
from rdflib import Namespace, URIRef, Literal, Graph
from rdflib.namespace import RDF, RDFS, XSD, DCTERMS
from bs4 import BeautifulSoup
from SPARQLWrapper import SPARQLWrapper, JSON, XML, JSONLD
import os
from flask_cors import CORS
from flasgger import Swagger

nlp = spacy.load("en_core_web_sm")

app = Flask(__name__)
Swagger(app)

CORS(app)

BLAZEGRAPH_URL = 'https://blazegraph-deploy-app.onrender.com/blazegraph/sparql'


def extract_entities(text):
    if text is None:
        return
    doc = nlp(text)
    entities = [(ent.text, ent.label_) for ent in doc.ents]
    return entities


def extract_topics(text):
    if text is None:
        return
    doc = nlp(text)
    topics = set()
    for ent in doc.ents:
        if ent.label_ in ["ORG", "GPE", "EVENT"]:
            topics.add(ent.text)
    return list(topics)


def fetch_news(api_key, country='us', category='general'):
    print("Getting news from API")
    url = f'https://newsapi.org/v2/top-headlines?country={country}&category={category}&apiKey={api_key}'
    response = requests.get(url)
    return response.json()


def scrape_multimedia(article_url):
    print(f"Scraping for multimedia from {article_url}")
    multimedia = []
    if article_url == "https://www.wzzm13.com/article/news/health/visitor-restrictions-mi-hospitals-surge-respiratory-illnesses/69-04827464-31dc-4e35-9c9a-eed9c90d3417" \
            or article_url == "https://www.wbir.com/article/news/local/tennessee-house-passes-amended-universal-school-voucher-bill/51-10d2cbf2-c8bc-4dae-a1f0-cca50d5ab8bc":
        return multimedia
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(article_url, headers=headers)

        soup = BeautifulSoup(response.text, 'html.parser')

        print("Start")

        for img in soup.find_all('img'):
            print(img)
            multimedia.append(img.get('src'))

        for video in soup.find_all('video'):
            print(video)
            multimedia.append(video.get('src'))

        print("Done")

    except Exception as e:
        print(f"Error scraping {article_url}: {e}")
    return multimedia


def get_dbpedia_topic_links(topic):
    print(f"Getting dbpedia topics for {topic}")
    if '"' in topic:
        return
    sparql = SPARQLWrapper("http://dbpedia.org/sparql")
    query = f"""
        SELECT ?resource WHERE {{
            ?resource rdfs:label "{topic}"@en.
        }}
    """
    sparql.setQuery(query)
    sparql.setReturnFormat(JSON)
    results = sparql.query().convert()
    return [result['resource']['value'] for result in results['results']['bindings']]


def store_news_in_db(articles):
    conn = sqlite3.connect('news.db')
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS news (
                        id INTEGER PRIMARY KEY, title TEXT, description TEXT, url TEXT, source TEXT, date TEXT, author, content, prev_img TEXT)
                   ''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS multimedia (
                        id INTEGER PRIMARY KEY, news_id INTEGER, url TEXT)
                   ''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS dbpedia_topics (
                        id INTEGER PRIMARY KEY, news_id INTEGER, topic TEXT, link TEXT)
                   ''')

    print("Tables created")

    for article in articles:
        cursor.execute('''INSERT INTO news (title, description, url, source, date, author, content, prev_img)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                       (article['title'], article['description'], article['url'], article['source']['name'],
                        article['publishedAt'], article['author'], article['content'], article['urlToImage']))
        news_id = cursor.lastrowid

        multimedia_links = scrape_multimedia(article['url'])
        for media in multimedia_links:
            cursor.execute('INSERT INTO multimedia (news_id, url) VALUES (?, ?)', (news_id, media))

        entities = extract_entities(article["description"])

        if article['description']:
            for topic in entities:
                links = get_dbpedia_topic_links(topic[0])
                if links:
                    for link in links:
                        cursor.execute('INSERT INTO dbpedia_topics (news_id, topic, link) VALUES (?, ?, ?)',
                                       (news_id, topic[0], link))

    conn.commit()
    conn.close()


def get_json_data():
    conn = sqlite3.connect('news.db')
    cursor = conn.cursor()

    response = []

    cursor.execute('SELECT id, title, description, url, source, date, author, content, prev_img FROM news')
    news_articles = cursor.fetchall()

    for news_id, title, description, url, source, date, author, content, prev_img in news_articles:

        topics = extract_topics(description)
        item_topics = []
        if topics is not None:
            for topic in topics:
                item_topics.append(topic)

        cursor.execute('SELECT url FROM multimedia WHERE news_id=?', (news_id,))
        multimedia_files = cursor.fetchall()
        multimedia_list = []
        for media_url, in multimedia_files:
            if media_url is not None:
                multimedia_list.append(media_url)

        cursor.execute('SELECT topic, link FROM dbpedia_topics WHERE news_id=?', (news_id,))
        dbpedia_topics = cursor.fetchall()
        dbpedia_topics_list = []
        for topic, link in dbpedia_topics:
            dbpedia_item = {
                "topic": topic,
                "dbpedia_url": link
            }
            dbpedia_topics_list.append(dbpedia_item)

        item = {
            "news_id": news_id,
            "title": title,
            "description": description,
            "author": author,
            "content": content,
            "url": url,
            "source": source,
            "date": date,
            "topics": item_topics,
            "preview_img": prev_img,
            "multimedia": multimedia_list,
            "dbpedia_topics": dbpedia_topics_list
        }

        response.append(item)

    conn.close()

    return response


def convert_to_rdf():
    print("Starting RDF conversion...")

    g = rdflib.Graph()

    DCTERMS = Namespace("http://purl.org/dc/terms/")
    IPTC = Namespace("http://iptc.org/std/Iptc4xmpCore/")
    FOAF = Namespace("http://xmlns.com/foaf/0.1/")
    DBPEDIA = Namespace("http://dbpedia.org/resource/")
    ssw = Namespace("http://www.socialsemanticweb.org/ns/")
    ns = Namespace("http://www.semanticweb.org/raressavin/ontologies/2025/0/NewsProv#")

    g.bind("dc", DCTERMS)
    g.bind("iptc", IPTC)
    g.bind("foaf", FOAF)
    g.bind("dbpedia", DBPEDIA)
    g.bind("news", ns)

    conn = sqlite3.connect('news.db')
    cursor = conn.cursor()

    cursor.execute('SELECT id, title, description, url, source, date, author, content, prev_img FROM news')
    news_articles = cursor.fetchall()

    for news_id, title, description, url, source, date, author, content, prev_img in news_articles:

        article_uri = ns[f'news{news_id}']

        cleaned_url = unquote(url)
        cleaned_url = re.sub(r'\\\\u003d', '=', cleaned_url)

        g.add((article_uri, RDF.type, ns.NewsArticle))
        g.add((article_uri, DCTERMS.title, Literal(title)))
        g.add((article_uri, DCTERMS.description, Literal(description)))
        g.add((article_uri, DCTERMS.source, Literal(source)))
        g.add((article_uri, DCTERMS.date, Literal(date, datatype=XSD.dateTime)))
        g.add((article_uri, DCTERMS.author, Literal(author)))
        g.add((article_uri, DCTERMS.content, Literal(content)))

        if prev_img is not None:
            encoded_prev_img = urllib.parse.quote(prev_img.encode('utf-8'), safe=":/")
            g.add((article_uri, DCTERMS.preview, URIRef(encoded_prev_img)))

        g.add((article_uri, DCTERMS.identifier, URIRef(cleaned_url)))

        topics = extract_topics(description)
        if topics is not None:
            for topic in topics:
                g.add((article_uri, ssw.topic, Literal(topic)))

        cursor.execute('SELECT url FROM multimedia WHERE news_id=?', (news_id,))
        multimedia_files = cursor.fetchall()
        for media_url, in multimedia_files:
            if media_url is not None:
                media_uri = ns[f'media{news_id}']
                g.add((media_uri, RDF.type, IPTC["Image"]))
                g.add((media_uri, IPTC["DigitalSourceType"], Literal("News Photo")))
                g.add((media_uri, IPTC["OrganisationInImage"], Literal(source)))
                encoded_url = urllib.parse.quote(media_url, safe=":/")
                g.add((media_uri, DCTERMS.identifier, URIRef(encoded_url)))
                g.add((article_uri, DCTERMS.hasPart, media_uri))

        cursor.execute('SELECT topic, link FROM dbpedia_topics WHERE news_id=?', (news_id,))
        dbpedia_topics = cursor.fetchall()
        for topic, link in dbpedia_topics:
            if "%" not in topic:
                topic_uri = DBPEDIA[topic.replace(" ", "_")]
                g.add((article_uri, DCTERMS.subject, topic_uri))
                g.add((topic_uri, DCTERMS.identifier, URIRef(link)))

    conn.close()

    g.serialize("newsDCMI.rdf", format="xml")

    print("RDF conversion completed. Data saved to 'newsDCMI.rdf'.")


@app.route('/news', methods=['GET'])
def get_news():
    """
        Retrieve all news articles.
        ---
        responses:
          200:
            description: A list of news articles
    """
    data = get_json_data()
    return jsonify(data)


@app.route('/news-page', methods=['GET'])
def get_news_page():
    """
       Retrieve paginated news articles.
       ---
       parameters:
         - name: skip
           in: query
           type: integer
           required: false
         - name: take
           in: query
           type: integer
           required: false
       responses:
         200:
           description: A paginated list of news articles
    """
    skip = request.args.get('skip', default=None, type=int)
    take = request.args.get('take', default=None, type=int)

    if skip is None and take is None:
        data = get_json_data()
    else:
        skip = skip if skip is not None else 0
        take = take if take is not None else 10
        data = get_json_data_page(skip, take)

    return jsonify(data)


def get_json_data_page(skip=0, take=10):
    conn = sqlite3.connect('news.db')
    cursor = conn.cursor()

    response = []

    cursor.execute(
        'SELECT id, title, description, url, source, date, author, content, prev_img FROM news LIMIT ? OFFSET ?',
        (take, skip) if take else ())
    news_articles = cursor.fetchall()

    for news_id, title, description, url, source, date, author, content, prev_img in news_articles:

        topics = extract_topics(description)
        item_topics = []
        if topics is not None:
            for topic in topics:
                item_topics.append(topic)

        cursor.execute('SELECT url FROM multimedia WHERE news_id=?', (news_id,))
        multimedia_files = cursor.fetchall()
        multimedia_list = []
        for media_url, in multimedia_files:
            if media_url is not None:
                multimedia_list.append(media_url)

        cursor.execute('SELECT topic, link FROM dbpedia_topics WHERE news_id=?', (news_id,))
        dbpedia_topics = cursor.fetchall()
        dbpedia_topics_list = []
        for topic, link in dbpedia_topics:
            dbpedia_item = {
                "topic": topic,
                "dbpedia_url": link
            }
            dbpedia_topics_list.append(dbpedia_item)

        item = {
            "news_id": news_id,
            "title": title,
            "description": description,
            "author": author,
            "content": content,
            "url": url,
            "source": source,
            "date": date,
            "topics": item_topics,
            "preview_img": prev_img,
            "multimedia": multimedia_list,
            "dbpedia_topics": dbpedia_topics_list
        }

        response.append(item)

    conn.close()

    return response


@app.route('/news/<int:news_id>', methods=['GET'])
def get_news_by_id(news_id):
    """
        Retrieve a news article by ID.
        ---
        parameters:
          - name: news_id
            in: path
            type: integer
            required: true
        responses:
          200:
            description: A news article
          404:
            description: News not found
    """
    conn = sqlite3.connect('news.db')
    cursor = conn.cursor()

    cursor.execute('SELECT id, title, description, url, source, date, author, content, prev_img FROM news WHERE id=?',
                   (news_id,))
    news = cursor.fetchone()

    if not news:
        return jsonify({"error": "News not found"}), 404

    news_id, title, description, url, source, date, author, content, prev_img = news

    topics = extract_topics(description)

    cursor.execute('SELECT url FROM multimedia WHERE news_id=?', (news_id,))
    multimedia_files = [media[0] for media in cursor.fetchall()]

    cursor.execute('SELECT topic, link FROM dbpedia_topics WHERE news_id=?', (news_id,))
    dbpedia_topics = [{"topic": topic, "dbpedia_url": link} for topic, link in cursor.fetchall()]

    conn.close()

    return jsonify({
        "news_id": news_id,
        "title": title,
        "description": description,
        "author": author,
        "content": content,
        "url": url,
        "source": source,
        "date": date,
        "topics": topics,
        "preview_img": prev_img,
        "multimedia": multimedia_files,
        "dbpedia_topics": dbpedia_topics
    })


@app.route("/news_rdfa/<int:news_id>", methods=['GET'])
def get_rdfa(news_id):
    """
        Retrieve a news article in RDFa format.
        ---
        parameters:
          - name: news_id
            in: path
            type: integer
            required: true
        responses:
          200:
            description: RDFa formatted news article
          404:
            description: News not found
    """
    conn = sqlite3.connect('news.db')
    cursor = conn.cursor()

    cursor.execute('SELECT id, title, description, url, source, date, author, content, prev_img FROM news WHERE id=?',
                   (news_id,))
    news = cursor.fetchone()

    if not news:
        return jsonify({"error": "News not found"}), 404

    news_id, title, description, url, source, date, author, content, prev_img = news

    topics = extract_topics(description)

    cursor.execute('SELECT url FROM multimedia WHERE news_id=?', (news_id,))
    multimedia_files = [media[0] for media in cursor.fetchall()]


    real_multimedia = []

    for item in multimedia_files:
        if item:
            real_multimedia.append(item)


    cursor.execute('SELECT topic, link FROM dbpedia_topics WHERE news_id=?', (news_id,))
    dbpedia_topics = [{"topic": topic, "dbpedia_url": link} for topic, link in cursor.fetchall()]

    conn.close()

    data = {
        "news_id": news_id,
        "title": title,
        "description": description,
        "author": author,
        "content": content,
        "url": url,
        "source": source,
        "date": date,
        "topics": topics,
        "preview_img": prev_img,
        "multimedia": real_multimedia,
        "dbpedia_topics": dbpedia_topics
    }

    return render_template("index.html", data=data)


def get_news_by_topic(topic):
    conn = sqlite3.connect('news.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT news_id FROM dbpedia_topics WHERE topic = ?
    ''', (topic,))

    results = cursor.fetchall()

    response = []
    seen_ids = []

    for result in results:
        cursor.execute(
            'SELECT DISTINCT id, title, description, url, source, date, author, content, prev_img FROM news WHERE id=?',
            (result[0],))
        news = cursor.fetchall()

        for news_id, title, description, url, source, date, author, content, prev_img in news:

            if news_id in seen_ids:
                continue

            seen_ids.append(news_id)

            topics = extract_topics(description)

            cursor.execute('SELECT url FROM multimedia WHERE news_id=?', (news_id,))
            multimedia_files = [media[0] for media in cursor.fetchall()]

            cursor.execute('SELECT topic, link FROM dbpedia_topics WHERE news_id=?', (news_id,))
            dbpedia_topics = [{"topic": topic, "dbpedia_url": link} for topic, link in cursor.fetchall()]

            response_item = {
                "news_id": news_id,
                "title": title,
                "description": description,
                "author": author,
                "content": content,
                "url": url,
                "source": source,
                "date": date,
                "topics": topics,
                "preview_img": prev_img,
                "multimedia": multimedia_files,
                "dbpedia_topics": dbpedia_topics
            }

            response.append(response_item)

    conn.close()

    return response


@app.route('/topics/<topic>', methods=['GET'])
def get_topic(topic):
    """
        Retrieve news articles by topic.
        ---
        parameters:
          - name: topic
            in: path
            type: string
            required: true
        responses:
          200:
            description: A list of news articles related to the topic
          404:
            description: No news found for the given topic
    """
    news_ids = get_news_by_topic(topic)

    if not news_ids:
        return jsonify({"message": "No news found for the given topic"}), 404

    return jsonify({"topic": topic, "news_list": news_ids})


@app.route('/recommend/<int:news_id>', methods=['GET'])
def recommend_news(news_id):
    """
        Get recommended news articles based on a given news article.
        ---
        parameters:
          - name: news_id
            in: path
            type: integer
            required: true
        responses:
          200:
            description: A list of recommended news articles
          404:
            description: News not found
    """
    conn = sqlite3.connect('news.db')
    cursor = conn.cursor()

    cursor.execute('SELECT description FROM news WHERE id=?', (news_id,))
    news = cursor.fetchone()

    if not news:
        return jsonify({"error": "News not found"}), 404

    description = news[0]
    topics = extract_topics(description)

    recommendations = []
    for topic in topics:
        cursor.execute('SELECT id, title, url FROM news WHERE id != ? AND description LIKE ?', (news_id, f'%{topic}%'))
        recommendations.extend([{"news_id": nid, "title": title, "url": url} for nid, title, url in cursor.fetchall()])

    conn.close()

    return jsonify({"recommended_news": recommendations})


@app.route('/news_rdf_turtle/<int:news_id>', methods=['GET'])
def get_news_rdf_by_id(news_id):
    """
    Retrieve a news article in RDF Turtle format.
    ---
    parameters:
      - name: news_id
        in: path
        type: integer
        required: true
    responses:
      200:
        description: RDF Turtle formatted news article
      404:
        description: News not found
    """
    g = rdflib.Graph()

    DCTERMS = rdflib.Namespace("http://purl.org/dc/terms/")
    IPTC = rdflib.Namespace("http://iptc.org/std/Iptc4xmpCore/")
    DBPEDIA = rdflib.Namespace("http://dbpedia.org/resource/")
    ssw = rdflib.Namespace("http://www.socialsemanticweb.org/ns/")
    ns = rdflib.Namespace("http://www.semanticweb.org/raressavin/ontologies/2025/0/NewsProv#")

    g.bind("dc", DCTERMS)
    g.bind("iptc", IPTC)
    g.bind("dbpedia", DBPEDIA)

    conn = sqlite3.connect('news.db')
    cursor = conn.cursor()

    cursor.execute('SELECT id, title, description, url, source, date, author, content, prev_img FROM news WHERE id=?',
                   (news_id,))
    news = cursor.fetchone()

    if not news:
        return jsonify({"error": "News not found"}), 404

    news_id, title, description, url, source, date, author, content, prev_img = news
    article_uri = ns[f'news{news_id}']

    cleaned_url = unquote(url)
    cleaned_url = re.sub(r'\\\\u003d', '=', cleaned_url)

    g.add((article_uri, rdflib.RDF.type, ns.NewsArticle))
    g.add((article_uri, DCTERMS.title, rdflib.Literal(title)))
    g.add((article_uri, DCTERMS.description, rdflib.Literal(description)))
    g.add((article_uri, DCTERMS.source, rdflib.Literal(source)))
    g.add((article_uri, DCTERMS.date, rdflib.Literal(date)))
    g.add((article_uri, DCTERMS.author, rdflib.Literal(author)))
    g.add((article_uri, DCTERMS.content, rdflib.Literal(content)))
    encoded_prev_img = urllib.parse.quote(prev_img, safe=":/")
    prev_img_uri = rdflib.URIRef(prev_img)
    g.add((prev_img_uri, DCTERMS.preview, rdflib.URIRef(encoded_prev_img)))
    g.add((article_uri, DCTERMS.identifier, rdflib.URIRef(cleaned_url)))

    topics = extract_topics(description)
    for topic in topics:
        g.add((article_uri, ssw.topic, rdflib.Literal(topic)))

    cursor.execute('SELECT url FROM multimedia WHERE news_id=?', (news_id,))
    for (media_url,) in cursor.fetchall():
        if media_url:
            media_uri = ns[f'media{news_id}']
            g.add((media_uri, rdflib.RDF.type, IPTC["Image"]))
            g.add((media_uri, IPTC["DigitalSourceType"], rdflib.Literal("News Photo")))
            encoded_url = urllib.parse.quote(media_url, safe=":/")
            g.add((media_uri, DCTERMS.identifier, rdflib.URIRef(encoded_url)))
            g.add((article_uri, DCTERMS.hasPart, media_uri))

    cursor.execute('SELECT topic, link FROM dbpedia_topics WHERE news_id=?', (news_id,))
    for topic, link in cursor.fetchall():
        topic_uri = DBPEDIA[topic.replace(" ", "_")]
        g.add((article_uri, DCTERMS.subject, topic_uri))
        g.add((topic_uri, DCTERMS.identifier, rdflib.URIRef(link)))

    conn.close()

    return g.serialize(format="turtle"), 200, {'Content-Type': 'text/turtle'}

@app.route('/news_rdf_xml/<int:news_id>', methods=['GET'])
def get_news_rdf_by_id_xml(news_id):
    """
        Retrieve a news article in RDF XML format.
        ---
        parameters:
          - name: news_id
            in: path
            type: integer
            required: true
        responses:
          200:
            description: RDF XML formatted news article
          404:
            description: News not found
    """
    g = rdflib.Graph()

    DCTERMS = rdflib.Namespace("http://purl.org/dc/terms/")
    IPTC = rdflib.Namespace("http://iptc.org/std/Iptc4xmpCore/")
    DBPEDIA = rdflib.Namespace("http://dbpedia.org/resource/")
    ssw = rdflib.Namespace("http://www.socialsemanticweb.org/ns/")
    ns = rdflib.Namespace("http://www.semanticweb.org/raressavin/ontologies/2025/0/NewsProv#")

    g.bind("dc", DCTERMS)
    g.bind("iptc", IPTC)
    g.bind("dbpedia", DBPEDIA)

    conn = sqlite3.connect('news.db')
    cursor = conn.cursor()

    cursor.execute('SELECT id, title, description, url, source, date, author, content, prev_img FROM news WHERE id=?',
                   (news_id,))
    news = cursor.fetchone()

    if not news:
        return jsonify({"error": "News not found"}), 404

    news_id, title, description, url, source, date, author, content, prev_img = news
    article_uri = ns[f'news{news_id}']

    cleaned_url = unquote(url)
    cleaned_url = re.sub(r'\\\\u003d', '=', cleaned_url)

    g.add((article_uri, rdflib.RDF.type, ns.NewsArticle))
    g.add((article_uri, DCTERMS.title, rdflib.Literal(title)))
    g.add((article_uri, DCTERMS.description, rdflib.Literal(description)))
    g.add((article_uri, DCTERMS.source, rdflib.Literal(source)))
    g.add((article_uri, DCTERMS.date, rdflib.Literal(date)))
    g.add((article_uri, DCTERMS.author, rdflib.Literal(author)))
    g.add((article_uri, DCTERMS.content, rdflib.Literal(content)))
    encoded_prev_img = urllib.parse.quote(prev_img, safe=":/")
    prev_img_uri = rdflib.URIRef(prev_img)
    g.add((prev_img_uri, DCTERMS.preview, rdflib.URIRef(encoded_prev_img)))
    g.add((article_uri, DCTERMS.identifier, rdflib.URIRef(cleaned_url)))

    topics = extract_topics(description)
    for topic in topics:
        g.add((article_uri, ssw.topic, rdflib.Literal(topic)))

    cursor.execute('SELECT url FROM multimedia WHERE news_id=?', (news_id,))
    for (media_url,) in cursor.fetchall():
        if media_url:
            media_uri = ns[f'media{news_id}']
            g.add((media_uri, rdflib.RDF.type, IPTC["Image"]))
            g.add((media_uri, IPTC["DigitalSourceType"], rdflib.Literal("News Photo")))
            encoded_url = urllib.parse.quote(media_url, safe=":/")
            g.add((media_uri, DCTERMS.identifier, rdflib.URIRef(encoded_url)))
            g.add((article_uri, DCTERMS.hasPart, media_uri))

    cursor.execute('SELECT topic, link FROM dbpedia_topics WHERE news_id=?', (news_id,))
    for topic, link in cursor.fetchall():
        topic_uri = DBPEDIA[topic.replace(" ", "_")]
        g.add((article_uri, DCTERMS.subject, topic_uri))
        g.add((topic_uri, DCTERMS.identifier, rdflib.URIRef(link)))

    conn.close()

    return g.serialize(format="xml"), 200, {'Content-Type': 'application/xml'}


FUSEKI_ENDPOINT = "https://fuseki-sparql.onrender.com/news/sparql"


@app.route('/sparql', methods=['POST'])
def sparql_query():
    """
            Execute a SPARQL query.
            ---
            tags:
              - SPARQL
            parameters:
              - name: query
                in: body
                required: true
                schema:
                  type: object
                  properties:
                    query:
                      type: string
                      description: SPARQL query string
                    format:
                      type: string
                      description: Response format (json, json-ld, rdfa)
                      enum: [json, json-ld, rdfa]
            responses:
              200:
                description: Query executed successfully
                schema:
                  type: object
                  properties:
                    source:
                      type: string
                    results:
                      type: object
              400:
                description: Bad request (missing query or unsupported format)
              500:
                description: Internal server error
            """
    try:
        data = request.get_json()
        if not data or 'query' not in data:
            return jsonify({"error": "No query provided"}), 400

        query = data['query']
        response_format = data.get('format', 'json')

        sparql = SPARQLWrapper(FUSEKI_ENDPOINT)
        sparql.setQuery(query)

        if response_format == 'json-ld':
            sparql.setReturnFormat(JSONLD)
        elif response_format == 'json':
            sparql.setReturnFormat(JSON)
        elif response_format == 'rdfa':
            sparql.setReturnFormat(JSON)
        else:
            return jsonify({"error": "Unsupported format"}), 400

        results = sparql.query().convert()

        if response_format == 'rdfa':
            news_links = []

            if "results" in results and "bindings" in results["results"]:
                for binding in results["results"]["bindings"]:
                    if "news" in binding:
                        news_uri = binding["news"]["value"]

                        parsed_uri = urlparse(news_uri)
                        news_id_with_prefix = parsed_uri.fragment

                        if news_id_with_prefix.startswith("news"):
                            news_id = news_id_with_prefix[4:]

                            if news_id:
                                news_links.append(f"https://flask-ontology-app.onrender.com/news_rdfa/{news_id}")

            if not news_links:
                news_links = "No news_id found in query results."

            return jsonify({
                "source": FUSEKI_ENDPOINT,
                "news_rdfa_links": news_links
            }), 200

        elif isinstance(results, Graph):
            jsonld_output = results.serialize(format='json-ld', indent=2)
            import json
            jsonld_data = json.loads(jsonld_output)

            for result in jsonld_data:
                if 'https://schema.org/author' in result:
                    result['https://schema.org/author'] = result['https://schema.org/author'][0]['@value']
                if 'https://schema.org/name' in result:
                    result['https://schema.org/name'] = result['https://schema.org/name'][0]['@value']

            return jsonify({
                "source": FUSEKI_ENDPOINT,
                "results": jsonld_data
            }), 200

        else:
            return jsonify({
                "source": FUSEKI_ENDPOINT,
                "results": results
            }), 200

    except Exception as e:
        return jsonify({
            "error": str(e),
            "source": FUSEKI_ENDPOINT
        }), 500


if __name__ == "__main__":

    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
