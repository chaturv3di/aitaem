# All Interesting Things Are Essentially Metrics
This is the `aitaem` library, pronounced "i-tame".

## Why?
> **TL;DR:** Point this Python library toward your OLAP database or a local CSV file and start generating insights, i.e. metrics, slices, segments, and time series, in no time. This library is LLM friendly (more on that later).

Business leaders, PMs, EMs, and even individual contributors constantly require deep understanding of their businesses and products. Another term for this understanding is "data insights". The most common way to obtain these insights is to rely on a data scientist or a business analyst to dive into the data and compute the insights. Why is this a bad idea?

1. Practically: Dashboards are limited in how much they can hold apriori. There's always a new question which existing dashboards cannot answer. There are always either too many dashboards or too few
2. Operationally: It is a waste of time if a DS or a BA has to dive into the source tables and (re)write SQL queries to compute customized metrics, slices, or segments repeatedly
3. Scientifically: The accuracy of ad-hoc analysis depends upon the individual; the same analysis done by different individuals can yield different results
4. Organisationally: Inter-org trust should be built upon _processes/toolings_ rather than on _individuals_

## What?
This library provides powerful functionality in a compact API. The core consists of two componenents.

1. Specifications: A simple declarative structure to modularly define metric specs, slice/breakdown specs, and segment specs
2. Computation: A small collection of Python classes with compact signatures which compute the metrics

Additionally, there are utilities to connect to various data backends (simultaneously) and helpers to visualize/render charts and trends.
