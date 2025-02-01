import spacy
import re
from urllib.parse import unquote
import urllib.parse
from flask import Flask, jsonify
import requests
import sqlite3
import rdflib
from bs4 import BeautifulSoup
from SPARQLWrapper import SPARQLWrapper, JSON

nlp = spacy.load("en_core_web_sm")

app = Flask(__name__)


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
                        id INTEGER PRIMARY KEY, title TEXT, description TEXT, url TEXT, source TEXT, date TEXT, author, content TEXT)
                   ''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS multimedia (
                        id INTEGER PRIMARY KEY, news_id INTEGER, url TEXT)
                   ''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS dbpedia_topics (
                        id INTEGER PRIMARY KEY, news_id INTEGER, topic TEXT, link TEXT)
                   ''')

    print("Tables created")

    for article in articles:
        cursor.execute('''INSERT INTO news (title, description, url, source, date, author, content)
                          VALUES (?, ?, ?, ?, ?, ?, ?)''',
                       (article['title'], article['description'], article['url'], article['source']['name'],
                        article['publishedAt'], article['author'], article['content']))
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

    cursor.execute('SELECT id, title, description, url, source, date, author, content FROM news')
    news_articles = cursor.fetchall()

    for news_id, title, description, url, source, date, author, content in news_articles:

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
            "multimedia": multimedia_list,
            "dbpedia_topics": dbpedia_topics_list
        }

        response.append(item)


    conn.close()

    return response



def convert_to_rdf():
    print("Starting RDF conversion...")
    g = rdflib.Graph()

    # Define namespaces
    DCTERMS = rdflib.Namespace("http://purl.org/dc/terms/")
    IPTC = rdflib.Namespace("http://iptc.org/std/Iptc4xmpCore/")
    FOAF = rdflib.Namespace("http://xmlns.com/foaf/0.1/")
    DBPEDIA = rdflib.Namespace("http://dbpedia.org/resource/")
    ssw = rdflib.Namespace("http://www.socialsemanticweb.org/ns/")
    ns = rdflib.Namespace("http://www.semanticweb.org/raressavin/ontologies/2025/0/NewsProv#")

    g.bind("dc", DCTERMS)
    g.bind("iptc", IPTC)
    g.bind("foaf", FOAF)
    g.bind("dbpedia", DBPEDIA)

    conn = sqlite3.connect('news.db')
    cursor = conn.cursor()

    cursor.execute('SELECT id, title, description, url, source, date, author, content FROM news')
    news_articles = cursor.fetchall()

    for news_id, title, description, url, source, date, author, content in news_articles:
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
        g.add((article_uri, DCTERMS.identifier, rdflib.URIRef(cleaned_url)))

        topics = extract_topics(description)
        if topics is not None:
            for topic in topics:
                g.add((article_uri, ssw.topic, rdflib.Literal(topic)))

        cursor.execute('SELECT url FROM multimedia WHERE news_id=?', (news_id,))
        multimedia_files = cursor.fetchall()
        for media_url, in multimedia_files:
            if media_url is not None:
                media_uri = ns[f'media{news_id}']
                g.add((media_uri, rdflib.RDF.type, IPTC["Image"]))
                g.add((media_uri, IPTC["DigitalSourceType"], rdflib.Literal("News Photo")))
                g.add((media_uri, IPTC["OrganisationInImage"], rdflib.Literal(source)))
                encoded_url = urllib.parse.quote(media_url, safe=":/")
                g.add((media_uri, DCTERMS.identifier, rdflib.URIRef(encoded_url)))

                g.add((article_uri, DCTERMS.hasPart, media_uri))

        cursor.execute('SELECT topic, link FROM dbpedia_topics WHERE news_id=?', (news_id,))
        dbpedia_topics = cursor.fetchall()
        for topic, link in dbpedia_topics:
            topic_uri = DBPEDIA[topic.replace(" ", "_")]
            g.add((article_uri, DCTERMS.subject, topic_uri))
            g.add((topic_uri, DCTERMS.identifier, rdflib.URIRef(link)))

    conn.close()

    g.serialize("newsDCMI.rdf", format="turtle")
    print("RDF serialization complete.")


@app.route('/news', methods=['GET'])
def get_news():
    data = get_json_data()
    return jsonify(data)


@app.route('/news/<int:news_id>', methods=['GET'])
def get_news_by_id(news_id):
    conn = sqlite3.connect('news.db')
    cursor = conn.cursor()

    cursor.execute('SELECT id, title, description, url, source, date, author, content FROM news WHERE id=?', (news_id,))
    news = cursor.fetchone()

    if not news:
        return jsonify({"error": "News not found"}), 404

    news_id, title, description, url, source, date, author, content = news

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
        "multimedia": multimedia_files,
        "dbpedia_topics": dbpedia_topics
    })


@app.route('/recommend/<int:news_id>', methods=['GET'])
def recommend_news(news_id):
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


@app.route('/news_rdf/<int:news_id>', methods=['GET'])
def get_news_rdf_by_id(news_id):
    g = rdflib.Graph()

    # Define namespaces
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

    cursor.execute('SELECT id, title, description, url, source, date, author, content FROM news WHERE id=?', (news_id,))
    news = cursor.fetchone()

    if not news:
        return jsonify({"error": "News not found"}), 404

    news_id, title, description, url, source, date, author, content = news
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


if __name__ == "__main__":
    # print("Starting")
    # API_KEY = "db3dcce0159647cd9292d9df2942c8a3"
    # news_data = fetch_news(API_KEY, 'de')
    # print(news_data)
    # print("News fetched")
    # store_news_in_db(news_data['articles'])
    # print("DB created")
    # convert_to_rdf()
    # print("Data saved and converted to RDF!")

    app.run(debug=True)
